import asyncio
import logging
import re
import sys
from datetime import datetime, timedelta
from typing import Optional

import aiosqlite
import pytz
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    ChatPermissions,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    User,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
ADMIN_ID = 123456789  # Замените на реальный ID админа (int)
TARGET_CHAT_ID = -1001234567890  # Замените на реальный ID чата (int)
TIMEZONE = 'Europe/Minsk'

# --- КОНСТАНТЫ ---
DB_NAME = "bot_database.db"
BAD_WORDS = ["плохоеслово", "мат", "запрещенка"]  # Добавьте сюда больше слов
WHITE_LIST_DOMAINS = ["youtube.com", "youtu.be", "twitch.tv", "t.me"]

# --- ЛОГИРОВАНИЕ ---
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
scheduler = AsyncIOScheduler(timezone=TIMEZONE)

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                points INTEGER DEFAULT 0
            )
            """
        )
        await db.commit()

async def add_points(user_id: int, points: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            """
            INSERT INTO users (user_id, points) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET points = points + ?
            """,
            (user_id, points, points),
        )
        await db.commit()

async def get_points(user_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT points FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

async def check_punishments(chat_id: int, user_id: int, current_points: int, message: Message):
    """Проверяет баллы и применяет наказания."""
    try:
        if current_points >= 10:
            await bot.ban_chat_member(chat_id, user_id)
            await message.answer(f"🚫 Пользователь {user_id} забанен навсегда (10+ баллов).")
        elif current_points >= 6:
            until_date = datetime.now() + timedelta(days=1)
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            await message.answer(f"🔇 Пользователь {user_id} заглушен на 1 день (6+ баллов).")
        elif current_points >= 3:
            until_date = datetime.now() + timedelta(minutes=30)
            await bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
            await message.answer(f"🔇 Пользователь {user_id} заглушен на 30 минут (3+ баллов).")
    except Exception as e:
        logger.error(f"Не удалось наказать пользователя {user_id}: {e}")

# --- ФИЛЬТРЫ И МОДЕРАЦИЯ ---
def check_bad_words(text: str) -> bool:
    if not text: return False
    text_lower = text.lower()
    for word in BAD_WORDS:
        if word in text_lower:
            return True
    return False

def check_links(text: str) -> bool:
    if not text: return False
    # Регулярное выражение для поиска ссылок
    urls = re.findall(r'(https?://\S+)', text)
    if not urls:
        return False
    
    for url in urls:
        is_allowed = False
        for domain in WHITE_LIST_DOMAINS:
            if domain in url:
                is_allowed = True
                break
        if not is_allowed:
            return True # Найдена запрещенная ссылка
    return False

@router.message(F.text)
async def message_handler(message: Message):
    # Пропускаем админов/владельца бота при проверке модерации
    if message.from_user.id == ADMIN_ID:
        return

    text = message.text
    violation_reason = ""
    points_to_add = 0

    # Проверка на запрещенные слова
    if check_bad_words(text):
        violation_reason = "Спам/Флуд (Запрещенные слова)"
        points_to_add = 1
    
    # Проверка ссылок
    elif check_links(text):
        violation_reason = "Реклама (Запрещенная ссылка)"
        points_to_add = 2

    if violation_reason:
        try:
            await message.delete()
        except Exception as e:
            logger.error(f"Не удалось удалить сообщение: {e}")
            return # Если не удалось удалить, продолжаем наказание.

        await add_points(message.from_user.id, points_to_add)
        new_points = await get_points(message.from_user.id)
        
        notification = await message.answer(
            f"⚠️ <b>Нарушение!</b>\n"
            f"Пользователь: {message.from_user.mention_html()}\n"
            f"Причина: {violation_reason}\n"
            f"Баллы: +{points_to_add} (Всего: {new_points})"
        )
        
        # Автоудаление уведомления через 10 секунд для чистоты чата
        await asyncio.sleep(10)
        try:
            await notification.delete()
        except:
            pass

        await check_punishments(message.chat.id, message.from_user.id, new_points, message)

# --- КОМАНДЫ АДМИНА ---
@router.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    
    is_silent = "-s" in (command.args or "")
    reply = message.reply_to_message
    if not reply:
        if not is_silent: await message.reply("Команду нужно использовать в ответ на сообщение.")
        return

    try:
        until_date = datetime.now() + timedelta(minutes=30)
        await bot.restrict_chat_member(
            message.chat.id,
            reply.from_user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until_date
        )
        if not is_silent:
            await message.answer(f"🔇 {reply.from_user.mention_html()} заглушен на 30 минут.")
    except Exception as e:
        logger.error(f"Ошибка мута: {e}")
        if not is_silent: await message.reply("Ошибка при муте.")

@router.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    is_silent = "-s" in (command.args or "")
    reply = message.reply_to_message
    if not reply:
        if not is_silent: await message.reply("Команду нужно использовать в ответ на сообщение.")
        return

    try:
        await bot.ban_chat_member(message.chat.id, reply.from_user.id)
        if not is_silent:
            await message.answer(f"🚫 {reply.from_user.mention_html()} забанен.")
    except Exception as e:
        logger.error(f"Ошибка бана: {e}")
        if not is_silent: await message.reply("Ошибка при бане.")

@router.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return

    is_silent = "-s" in (command.args or "")
    reply = message.reply_to_message
    if not reply:
        if not is_silent: await message.reply("Команду нужно использовать в ответ на сообщение.")
        return

    try:
        # Ограничение со всеми разрешениями фактически является размутом
        await bot.restrict_chat_member(
            message.chat.id,
            reply.from_user.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True
            )
        )
        if not is_silent:
            await message.answer(f"🔊 {reply.from_user.mention_html()} размучен.")
    except Exception as e:
        logger.error(f"Ошибка размута: {e}")
        if not is_silent: await message.reply("Ошибка при размуте.")

@router.message(Command("info"))
async def cmd_info(message: Message, command: CommandObject):
    if not is_admin(message.from_user.id):
        return
    
    # Требование: "В ЛС бота: /info @username"
    
    if not command.args:
        await message.reply("Использование: /info @username")
        return

    username = command.args.replace("@", "").strip()
    # Нам нужно найти user_id по username. Боты не могут легко разрешить username в ID.
    # Предположим, что админ предоставляет ID.
    
    target_id = None
    if username.isdigit():
        target_id = int(username)
    else:
        await message.reply("⚠️ Пожалуйста, укажите ID пользователя (числом). Поиск по username не поддерживается без базы данных пользователей.")
        return

    points = await get_points(target_id)
    await message.answer(f"ℹ️ Информация о пользователе:\nID: {target_id}\nБаллы нарушений: {points}")

# --- СИСТЕМА РЕПОРТОВ ---
@router.message(Command("report"))
async def cmd_report(message: Message):
    reply = message.reply_to_message
    if not reply:
        await message.reply("Используйте команду в ответ на сообщение нарушителя.")
        return

    # Отправка в ЛС админу
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Мут 30м", callback_data=f"rep_mute_30_{reply.from_user.id}_{reply.message_id}_{message.chat.id}"),
            InlineKeyboardButton(text="Бан", callback_data=f"rep_ban_{reply.from_user.id}_{reply.message_id}_{message.chat.id}")
        ],
        [
            InlineKeyboardButton(text="Удалить сообщение", callback_data=f"rep_del_{reply.from_user.id}_{reply.message_id}_{message.chat.id}"),
            InlineKeyboardButton(text="Игнорировать", callback_data="rep_ignore")
        ]
    ])

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>Жалоба!</b>\n"
            f"От: {message.from_user.mention_html()}\n"
            f"На: {reply.from_user.mention_html()} (ID: {reply.from_user.id})\n"
            f"Чат: {message.chat.title}\n"
            f"Текст: {reply.text}",
            reply_markup=keyboard
        )
        await message.answer("Жалоба отправлена админу.")
    except Exception as e:
        logger.error(f"Не удалось отправить жалобу: {e}")
        await message.answer("Не удалось отправить жалобу (возможно, у админа закрыта личка).")

@router.callback_query(F.data.startswith("rep_"))
async def callback_report(callback: CallbackQuery):
    action = callback.data.split("_")[1]
    
    if action == "ignore":
        await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>Решение: Игнорировать</b>", reply_markup=None)
        await callback.answer("Жалоба проигнорирована.")
        return

    # Парсинг данных
    try:
        parts = callback.data.split("_")
        # Формат: rep_action_userId_msgId_chatId
        if action == "mute":
            target_id = int(parts[3])
            msg_id = int(parts[4])
            chat_id = int(parts[5])
        else:
            target_id = int(parts[2])
            msg_id = int(parts[3])
            chat_id = int(parts[4])
    except IndexError:
        await callback.answer("Ошибка данных кнопки.")
        return

    try:
        if action == "mute":
            until_date = datetime.now() + timedelta(minutes=30)
            await bot.restrict_chat_member(
                chat_id,
                target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date
            )
            await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>Решение: Мут 30 мин</b>", reply_markup=None)
            await bot.send_message(chat_id, f"🔇 Пользователь {target_id} заглушен по жалобе.")
        
        elif action == "ban":
            await bot.ban_chat_member(chat_id, target_id)
            await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>Решение: Бан</b>", reply_markup=None)
            await bot.send_message(chat_id, f"🚫 Пользователь {target_id} забанен по жалобе.")
        
        elif action == "del":
            await bot.delete_message(chat_id, msg_id)
            await callback.message.edit_text(f"{callback.message.text}\n\n✅ <b>Решение: Сообщение удалено</b>", reply_markup=None)
            
    except Exception as e:
        logger.error(f"Ошибка действия репорта: {e}")
        await callback.answer(f"Ошибка выполнения: {e}")

# --- ПЛАНИРОВЩИК ---
async def open_chat():
    try:
        await bot.set_chat_permissions(
            TARGET_CHAT_ID,
            ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
                can_send_polls=True,
                can_invite_users=True
            )
        )
        await bot.send_message(TARGET_CHAT_ID, "☀️ <b>Чат открыт!</b> Доброе утро.")
        logger.info("Чат открыт.")
    except Exception as e:
        logger.error(f"Не удалось открыть чат: {e}")

async def close_chat():
    try:
        await bot.set_chat_permissions(
            TARGET_CHAT_ID,
            ChatPermissions(
                can_send_messages=False
            )
        )
        await bot.send_message(TARGET_CHAT_ID, "🌙 <b>Чат закрыт!</b> До 07:00.")
        logger.info("Чат закрыт.")
    except Exception as e:
        logger.error(f"Не удалось закрыть чат: {e}")

async def check_time_on_startup():
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    # Ночное время между 23:00 и 07:00
    if now.hour >= 23 or now.hour < 7:
        logger.info("Запуск: Сейчас ночь. Закрываем чат.")
        await close_chat()
    else:
        logger.info("Запуск: Сейчас день. Убеждаемся, что чат открыт.")
        pass

# --- MAIN ---
async def main():
    await init_db()
    
    dp.include_router(router)
    
    # Настройка планировщика
    scheduler.add_job(close_chat, 'cron', hour=23, minute=0)
    scheduler.add_job(open_chat, 'cron', hour=7, minute=0)
    scheduler.start()
    
    # Проверка при запуске
    await check_time_on_startup()
    
    logger.info("Бот запущен...")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен!")
