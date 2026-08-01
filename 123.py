#!/usr/bin/env python3
"""
Многофункциональный Telegram-бот для чатов.
Репорт-голосование, ИИ-ответы (Cerebras Gemma 4 31B), погода, детальная статистика пользователя,
кастомные фразы для группы, история чата.
Требования: pip install python-telegram-bot requests
"""

import logging
import sqlite3
import random
import time
import math
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from telegram import Update, ChatMemberUpdated
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

# ---------- Настройки ----------
BOT_TOKEN = "токен бота"
GLOBAL_ADMIN_ID = "пиши сюда айди админа
AI_API_KEY = "айпи ИИ"
AI_MODEL = "gemma-4-31b"
AI_API_URL = "https://api.cerebras.ai/v1/chat/completions"
AI_MAX_REQUESTS_PER_MINUTE = 25

DB_FILE = "bot_data.db"

# ID целевой группы и пользователей
CUSTOM_GROUP_ID = "айди группы"
POLINA_ID = "оставь пустым" 

# Фиксированная длительность мута по репортам (минут)
REPORT_MUTE_MINUTES = 15

# Мат-слова (русские)
MAT_WORDS = [
    "бля", "хуй", "пизда", "ебать", "еблан", "мудак", "сука",
    "нах", "заеб", "выеб", "долбо", "пидор", "гондон", "хер", "сра",
    "жопа", "говно", "чмо", "урод",
]

# Кастомные фразы для группы
CUSTOM_PHRASES = [
    "Пасик Егорки у Полины, верни животина!",
    "Где пасик, Полина? Верни его Егорке!",
    "Полина, верни пасик Егорки, а то Илюха без пасика остался",
    "Егоркин пасик всё ещё у Полины, это пиздец",
    "Полина, отдай пасик, ёбаный в рот",
    "Слышь, Полина, пасик у тебя, не мути",
    "Пасик Егорки скучает, верни его!",
    "Полина - пасикодержательница, отдай",
    "Егор без пасика, Илюха без пасика, а у Полины целый зоопарк",
    "Пасик, сука, у Полины!",
    "Илюха больше без пасика, жесть",
    "Илюха потерял пасика, теперь грустит",
    "Илюха теперь бедный, без пасика",
    "Илюха, где твой пасик? Ах да, у Полины",
    "Илюха без пасика - это пиздец",
    "Илюха, верни свой пасик, бля",
    "Степа встречался с давалкой, но не поебался, лол",
    "Степа и давалка - история любви без ебли",
    "Степа: давалка была, а секса нет",
    "Степа помнит давалку, но без продолжения",
    "Степа в пролёте с давалкой, ахах",
    "Степа, ты давалку хоть поебал? Неа",
    "Степа и его неудачный роман с давалкой",
    "В этом чате всё через жопу, но весело",
    "Пасик – главная валюта этого чата",
    "Без пасика жизнь не та",
    "Давалка Степы стала легендой",
]

# ---------- Ограничение запросов AI ----------
ai_call_times = deque(maxlen=AI_MAX_REQUESTS_PER_MINUTE)

def can_ai_request() -> bool:
    now = time.time()
    while ai_call_times and now - ai_call_times[0] > 60:
        ai_call_times.popleft()
    return len(ai_call_times) < AI_MAX_REQUESTS_PER_MINUTE

def register_ai_request():
    ai_call_times.append(time.time())

# ---------- База данных ----------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # Таблица администраторов чата (регистрация создателя)
    c.execute("""CREATE TABLE IF NOT EXISTS chat_admins (
        chat_id INTEGER,
        user_id INTEGER,
        role TEXT,
        PRIMARY KEY (chat_id, user_id)
    )""")
    # Таблица репортов (голосов)
    c.execute("""CREATE TABLE IF NOT EXISTS reports (
        chat_id INTEGER,
        target_id INTEGER,
        voter_id INTEGER,
        PRIMARY KEY (chat_id, target_id, voter_id)
    )""")
    # Таблица активных мутов (для возможного отслеживания, сейчас не используется для снятия, но оставим)
    c.execute("""CREATE TABLE IF NOT EXISTS active_mutes (
        chat_id INTEGER,
        user_id INTEGER,
        until TEXT,
        PRIMARY KEY (chat_id, user_id)
    )""")
    # Лог сообщений для аналитики и статистики
    c.execute("""CREATE TABLE IF NOT EXISTS message_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        user_id INTEGER,
        message_id INTEGER,
        text TEXT,
        reply_to_message_id INTEGER,
        timestamp REAL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_log_chat ON message_log(chat_id, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_msg_log_user ON message_log(chat_id, user_id, timestamp)")
    conn.commit()
    conn.close()

init_db()

# Хранилище контекста ИИ и флагов
chat_contexts = defaultdict(lambda: deque(maxlen=10))
polina_first_msg = defaultdict(bool)

def get_db():
    return sqlite3.connect(DB_FILE)

def is_global_admin(user_id: int) -> bool:
    return user_id == GLOBAL_ADMIN_ID

async def is_bot_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет зарегистрирован ли пользователь как создатель бота."""
    if is_global_admin(user_id):
        return True
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT user_id FROM chat_admins WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = c.fetchone()
    conn.close()
    return row is not None

# ---------- Вспомогательные функции ----------
async def resolve_user_from_arg(bot, arg: str) -> Optional[int]:
    """Получить user_id из @username или числового ID."""
    if arg.startswith("@"):
        username = arg[1:]
        try:
            chat = await bot.get_chat(f"@{username}")
            if chat.type == "private":
                return chat.id
        except Exception:
            pass
    else:
        try:
            uid = int(arg)
            return uid
        except ValueError:
            pass
    return None

async def mute_user(bot, chat_id: int, user_id: int, minutes: int) -> bool:
    """Замутить пользователя на указанное число минут."""
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    permissions = ChatPermissions(can_send_messages=False)
    try:
        await bot.restrict_chat_member(chat_id, user_id, permissions, until_date=until)
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO active_mutes (chat_id, user_id, until) VALUES (?,?,?)",
                  (chat_id, user_id, until.isoformat()))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logging.error(f"Не удалось замутить {user_id}: {e}")
        return False

def contains_mat(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(w in text_lower for w in MAT_WORDS)

def save_message(chat_id, user_id, message_id, text, reply_to_message_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO message_log (chat_id, user_id, message_id, text, reply_to_message_id, timestamp) VALUES (?,?,?,?,?,?)",
              (chat_id, user_id, message_id, text, reply_to_message_id, time.time()))
    # Ограничиваем количество записей на чат
    c.execute("DELETE FROM message_log WHERE chat_id=? AND id NOT IN (SELECT id FROM message_log WHERE chat_id=? ORDER BY id DESC LIMIT 2000)", (chat_id, chat_id))
    conn.commit()
    conn.close()

def get_ai_context(chat_id: int) -> List[dict]:
    """Собирает контекст для AI: последние сообщения + популярные."""
    conn = get_db()
    c = conn.cursor()
    # Последние 5 сообщений
    c.execute("SELECT text, user_id FROM message_log WHERE chat_id=? ORDER BY id DESC LIMIT 5", (chat_id,))
    last_msgs = c.fetchall()[::-1]
    # Сообщения с наибольшим количеством ответов
    c.execute("SELECT text, COUNT(*) as cnt FROM message_log WHERE chat_id=? AND reply_to_message_id IS NOT NULL GROUP BY text ORDER BY cnt DESC LIMIT 5", (chat_id,))
    top_msgs = c.fetchall()
    conn.close()
    context = []
    for text, user_id in last_msgs:
        if text:
            context.append({"role": "user", "content": text[:200], "name": str(user_id)})
    for text, cnt in top_msgs:
        if text and text not in [m["content"] for m in context]:
            context.append({"role": "user", "content": f"[Популярное: {cnt} ответов] {text[:200]}", "name": "chat"})
    return context[-10:]

async def get_active_users_count(chat_id: int, hours: int = 12) -> int:
    """Количество уникальных пользователей за последние N часов."""
    since = time.time() - hours * 3600
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM message_log WHERE chat_id=? AND timestamp >= ?", (chat_id, since))
    count = c.fetchone()[0]
    conn.close()
    return count

# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот для чатов.\n"
        "Доступные команды:\n"
        "/help - список команд\n"
        "/rep (ответ или @username) - репорт (голосование)\n"
        "/info (ответ) - информация о пользователе\n"
        "/weather <город> - погода\n"
        "/clearai - очистить историю общения с ИИ\n"
        "/stats - статистика репортов"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

# --- Репорты (голосование с порогом = половина активных) ---
async def cmd_rep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    voter_id = update.effective_user.id
    target_id = None
    target_mention = None

    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        target_id = target.id
        target_mention = target.mention_html()
    elif context.args:
        arg = context.args[0]
        target_id = await resolve_user_from_arg(context.bot, arg)
        if target_id:
            target_mention = arg
        else:
            await update.message.reply_text("Не удалось найти пользователя. Используйте @username или ID.")
            return
    else:
        await update.message.reply_text("Ответьте на сообщение или укажите @username/ID.")
        return

    if target_id == context.bot.id:
        await update.message.reply_text("Нельзя жаловаться на бота.")
        return
    if target_id == voter_id:
        await update.message.reply_text("Нельзя жаловаться на себя.")
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM reports WHERE chat_id=? AND target_id=? AND voter_id=?",
              (chat_id, target_id, voter_id))
    if c.fetchone()[0] > 0:
        conn.close()
        await update.message.reply_text("Вы уже голосовали.")
        return

    c.execute("INSERT INTO reports (chat_id, target_id, voter_id) VALUES (?,?,?)",
              (chat_id, target_id, voter_id))
    conn.commit()
    c.execute("SELECT COUNT(*) FROM reports WHERE chat_id=? AND target_id=?", (chat_id, target_id))
    count = c.fetchone()[0]
    conn.close()

    active = await get_active_users_count(chat_id, 12)
    threshold = max(1, math.ceil(active / 2)) if active > 0 else 1

    if count >= threshold:
        success = await mute_user(context.bot, chat_id, target_id, REPORT_MUTE_MINUTES)
        if success:
            await update.message.reply_text(
                f"Пользователь {target_mention} набрал {count}/{threshold} голосов и замучен на {REPORT_MUTE_MINUTES} мин.",
                parse_mode=ParseMode.HTML)
            # Очищаем голоса
            conn = get_db()
            c = conn.cursor()
            c.execute("DELETE FROM reports WHERE chat_id=? AND target_id=?", (chat_id, target_id))
            conn.commit()
            conn.close()
        else:
            await update.message.reply_text("Не удалось замутить пользователя.")
    else:
        await update.message.reply_text(
            f"Репорт на {target_mention} принят. Нужно {threshold} голосов (активных: {active}). "
            f"Текущий счёт: {count}/{threshold}.",
            parse_mode=ParseMode.HTML)

# --- Информация о пользователе ---
async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not update.message.reply_to_message:
        await update.message.reply_text("Ответьте на сообщение пользователя.")
        return
    user = update.message.reply_to_message.from_user
    user_id = user.id

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT MIN(timestamp) FROM message_log WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    first_ts = c.fetchone()[0]
    c.execute("SELECT MAX(timestamp) FROM message_log WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    last_ts = c.fetchone()[0]

    now_ts = time.time()
    day_ago = now_ts - 86400
    week_ago = now_ts - 604800
    month_ago = now_ts - 2592000  # 30 дней
    c.execute("SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=? AND timestamp >= ?", (chat_id, user_id, day_ago))
    day_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=? AND timestamp >= ?", (chat_id, user_id, week_ago))
    week_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=? AND timestamp >= ?", (chat_id, user_id, month_ago))
    month_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM message_log WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    total_count = c.fetchone()[0]
    conn.close()

    def format_date(ts):
        if ts:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
            return dt.strftime("%d.%m.%Y")
        return "неизвестно"

    def format_ago(ts):
        if not ts:
            return "никогда"
        delta = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
        if delta.days > 30:
            months = delta.days // 30
            days = delta.days % 30
            return f"{months} мес {days} дн"
        elif delta.days > 0:
            return f"{delta.days} дн {delta.seconds // 3600} ч"
        else:
            hours = delta.seconds // 3600
            mins = (delta.seconds % 3600) // 60
            return f"{hours} ч {mins} мин"

    first_str = format_date(first_ts)
    first_ago = format_ago(first_ts) if first_ts else "никогда"
    last_ago = format_ago(last_ts)

    text = (
        f"Первое появление: {first_str} ({first_ago})\n"
        f"Последний актив: {last_ago}\n"
        f"Актив (д|н|м|весь): {day_count} | {week_count} | {month_count} | {total_count}\n"
        f"ID: {user.id}\n"
        f"Имя: {user.full_name}\n"
        f"Username: @{user.username if user.username else 'нет'}"
    )
    await update.message.reply_text(text)

# --- Очистка контекста ИИ ---
async def cmd_clearai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_contexts.pop(chat_id, None)
    await update.message.reply_text("История ИИ очищена.")

# --- Статистика репортов ---
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT target_id, COUNT(*) as cnt FROM reports WHERE chat_id=? GROUP BY target_id ORDER BY cnt DESC LIMIT 10", (chat_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("Нет активных репортов.")
        return
    text = "Топ репортов:\n"
    for target_id, cnt in rows:
        text += f"ID {target_id}: {cnt} голосов\n"
    await update.message.reply_text(text)

# --- Погода ---
GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

async def cmd_weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите город: /weather Москва")
        return
    city = " ".join(context.args)
    try:
        params = {"q": city, "format": "json", "limit": 1}
        headers = {"User-Agent": "TelegramBot/1.0"}
        resp = requests.get(GEOCODE_URL, params=params, headers=headers, timeout=10)
        data = resp.json()
        if not data:
            await update.message.reply_text("Город не найден.")
            return
        lat, lon = data[0]["lat"], data[0]["lon"]
    except Exception:
        await update.message.reply_text("Ошибка геокодирования.")
        return
    try:
        wresp = requests.get(WEATHER_URL, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "forecast_days": 1, "timezone": "auto"
        }, timeout=10)
        wdata = wresp.json()
        temps = wdata["hourly"]["temperature_2m"]
        temp = temps[min(12, len(temps)-1)]
        await update.message.reply_text(f"Погода в {city}: {temp}°C")
    except Exception:
        await update.message.reply_text("Ошибка получения погоды.")

# --- Обработка сообщений (кастомные фразы, логирование, ИИ) ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    chat_id = update.effective_chat.id
    user = msg.from_user
    user_id = user.id

    # Логирование
    reply_id = msg.reply_to_message.message_id if msg.reply_to_message else None
    save_message(chat_id, user_id, msg.message_id, text, reply_id)

    # Кастомные фразы для целевой группы
    if chat_id == CUSTOM_GROUP_ID:
        if user_id == POLINA_ID and not polina_first_msg[chat_id]:
            polina_first_msg[chat_id] = True
            await msg.reply_text("верни пасик")
        elif user_id == POLINA_ID and random.randint(1, 100) == 1:
            phrase = random.choice([p for p in CUSTOM_PHRASES if "пасик" in p.lower()])
            await msg.reply_text(phrase)
        elif random.randint(1, 75) == 1:
            await msg.reply_text(random.choice(CUSTOM_PHRASES))

    if text.startswith("/"):
        return

    # ИИ-ответы (всегда включены в группах при упоминании, в ЛС всегда)
    if msg.chat.type != "private":
        bot_username = context.bot.username
        mentioned = f"@{bot_username}" in text
        reply_to_bot = msg.reply_to_message and msg.reply_to_message.from_user.id == context.bot.id
        if not (mentioned or reply_to_bot):
            return

    if not can_ai_request():
        return
    register_ai_request()

    ctx = get_ai_context(chat_id)
    ctx.append({"role": "user", "content": text[:200], "name": user.full_name})

    recent_texts = " ".join([m["content"] for m in ctx if m["role"] == "user"])
    has_mat = contains_mat(recent_texts)

    system_prompt = (
        "Ты — живой человек в Telegram-чате. Отвечай кратко (до 50 слов), разговорным стилем, без смайликов. "
        "Если в разговоре матерятся, ты тоже можешь материться."
    )

    messages = [{"role": "system", "content": system_prompt}]
    for m in ctx:
        role = "assistant" if m["role"] == "assistant" else "user"
        messages.append({"role": role, "content": f"{m['name']}: {m['content']}"})

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": AI_MODEL, "messages": messages, "max_tokens": 150, "temperature": 0.7}
    try:
        resp = requests.post(AI_API_URL, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        reply_text = data["choices"][0]["message"]["content"].strip()[:500]
        await msg.reply_text(reply_text)
    except Exception as e:
        logging.error(f"AI error: {e}")

# --- Регистрация создателя при добавлении бота ---
async def on_bot_added(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if update.my_chat_member.new_chat_member.status == ChatMemberStatus.MEMBER:
        try:
            admins = await context.bot.get_chat_administrators(chat.id)
            for admin in admins:
                if admin.status == ChatMemberStatus.OWNER:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("INSERT OR IGNORE INTO chat_admins (chat_id, user_id, role) VALUES (?,?,?)",
                              (chat.id, admin.user.id, "creator"))
                    conn.commit()
                    conn.close()
                    logging.info(f"Creator {admin.user.id} registered in chat {chat.id}")
                    break
        except Exception as e:
            logging.error(f"Failed to get admins: {e}")

# ---------- Точка входа ----------
def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("rep", cmd_rep))
    application.add_handler(CommandHandler("info", cmd_info))
    application.add_handler(CommandHandler("clearai", cmd_clearai))
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("weather", cmd_weather))

    application.add_handler(ChatMemberHandler(on_bot_added, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
