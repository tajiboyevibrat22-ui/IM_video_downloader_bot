import os
import re
import json
import time
import sqlite3
import logging
import tempfile
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import telebot
from telebot import types
from flask import Flask, request, abort
from yt_dlp import YoutubeDL

# =========================
# SOZLAMALAR
# =========================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "/tmp/bot.db")

CHANNEL = "@Yangiyil_27"
WEBHOOK_PATH = "/telegram-webhook"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables ichida yo'q.")

if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError("ADMIN_ID raqam bo'lishi kerak.")

ADMIN_ID = int(ADMIN_ID_RAW)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)

db_lock = Lock()
broadcast_waiting = set()


# =========================
# MA'LUMOTLAR BAZASI
# =========================

def db_connect():
    return sqlite3.connect(DB_PATH, timeout=20)


def init_db():
    with db_lock:
        with db_connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()


def save_user(user):
    with db_lock:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
            """, (
                user.id,
                user.username or "",
                user.first_name or ""
            ))
            conn.commit()


def get_user_ids():
    with db_lock:
        with db_connect() as conn:
            rows = conn.execute(
                "SELECT user_id FROM users"
            ).fetchall()
    return [row[0] for row in rows]


def get_user_count():
    with db_lock:
        with db_connect() as conn:
            result = conn.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()
    return result[0]


init_db()


# =========================
# BOT BUYRUQLARI
# =========================

def setup_commands():
    try:
        # Oddiy foydalanuvchilar menyusida faqat /start
        bot.set_my_commands([
            types.BotCommand("start", "Botni ishga tushirish")
        ])

        # Admin uchun qo'shimcha buyruqlar
        bot.set_my_commands(
            [
                types.BotCommand("start", "Botni ishga tushirish"),
                types.BotCommand("admin", "Admin panel"),
                types.BotCommand("help", "Yordam")
            ],
            scope=types.BotCommandScopeChat(chat_id=ADMIN_ID)
        )

        logger.info("Bot buyruqlari sozlandi.")
    except Exception:
        logger.exception("Buyruqlar menyusini sozlashda xato.")


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================

def is_admin(user_id):
    return user_id == ADMIN_ID


def is_instagram_url(text):
    if not text:
        return False

    try:
        parsed = urlparse(text.strip())
        host = (parsed.hostname or "").lower()

        return (
            parsed.scheme in ("http", "https")
            and (
                host == "instagram.com"
                or host.endswith(".instagram.com")
            )
        )
    except Exception:
        return False


def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        logger.exception("Kanal obunasini tekshirishda xato.")
        return None


def subscription_keyboard():
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(
        types.InlineKeyboardButton(
            "📢 Kanalga obuna bo'lish",
            url="https://t.me/Yangiyil_27"
        )
    )
    keyboard.add(
        types.InlineKeyboardButton(
            "✅ Obunani tekshirish",
            callback_data="check_subscription"
        )
    )
    return keyboard


def send_start_message(chat_id):
    bot.send_message(
        chat_id,
        "Salom! 👋\n\n"
        "Instagram video, Reels, rasm va carousel havolasini yuboring.\n\n"
        "Eslatma: ayrim yopiq yoki login talab qiladigan postlar "
        "yuklanmasligi mumkin."
    )


# =========================
# /START
# =========================

@bot.message_handler(commands=["start"])
def start_handler(message):
    save_user(message.from_user)

    if is_admin(message.from_user.id):
        send_start_message(message.chat.id)
        return

    subscribed = check_subscription(message.from_user.id)

    if subscribed is True:
        send_start_message(message.chat.id)
    elif subscribed is False:
        bot.send_message(
            message.chat.id,
            "Botdan foydalanish uchun avval kanalga obuna bo'ling.",
            reply_markup=subscription_keyboard()
        )
    else:
        bot.send_message(
            message.chat.id,
            "Hozir obunani tekshira olmadim. Birozdan keyin qayta urinib ko'ring."
        )


# =========================
# OBUNANI TEKSHIRISH
# =========================

@bot.callback_query_handler(
    func=lambda call: call.data == "check_subscription"
)
def subscription_callback(call):
    try:
        subscribed = check_subscription(call.from_user.id)

        if subscribed is True:
            bot.answer_callback_query(
                call.id, "Obuna tasdiqlandi!"
            )
            bot.send_message(
                call.message.chat.id,
                "Rahmat! Endi botdan foydalanishingiz mumkin."
            )
            send_start_message(call.message.chat.id)

        elif subscribed is False:
            bot.answer_callback_query(
                call.id,
                "Avval kanalga obuna bo'ling.",
                show_alert=True
            )

        else:
            bot.answer_callback_query(
                call.id,
                "Tekshirishda xato. Keyinroq urinib ko'ring.",
                show_alert=True
            )
    except Exception:
        logger.exception("Obuna callback xatosi.")


# =========================
# ADMIN PANEL
# =========================

@bot.message_handler(commands=["admin"])
def admin_handler(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bu buyruq faqat admin uchun.")
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(
        types.InlineKeyboardButton(
            "👥 Foydalanuvchilar",
            callback_data="admin_users"
        )
    )
    keyboard.row(
        types.InlineKeyboardButton(
            "📨 Xabar yuborish",
            callback_data="admin_broadcast"
        )
    )

    bot.send_message(
        message.chat.id,
        "🛠 ADMIN PANEL\n\nKerakli bo'limni tanlang:",
        reply_markup=keyboard
    )


@bot.message_handler(commands=["help"])
def help_handler(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Bu buyruq faqat admin uchun.")
        return

    bot.send_message(
        message.chat.id,
        "ADMIN YORDAMI\n\n"
        "/start — botni ishga tushirish\n"
        "/admin — admin panel\n"
        "/help — yordam\n\n"
        "Ommaviy xabar yuborish uchun admin paneldan foydalaning."
    )


@bot.callback_query_handler(
    func=lambda call: call.data in (
        "admin_users", "admin_broadcast"
    )
)
def admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Ruxsat yo'q.", show_alert=True
        )
        return

    if call.data == "admin_users":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"👥 Bazadagi foydalanuvchilar: {get_user_count()}"
        )

    elif call.data == "admin_broadcast":
        broadcast_waiting.add(call.from_user.id)
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "Yuboriladigan xabarni jo'nating.\n"
            "Bekor qilish uchun /cancel yozing."
        )


@bot.message_handler(commands=["cancel"])
def cancel_handler(message):
    if is_admin(message.from_user.id):
        broadcast_waiting.discard(message.from_user.id)
        bot.send_message(message.chat.id, "Amal bekor qilindi.")


# =========================
# OMMAVIY XABAR
# =========================

def broadcast_message(message):
    user_ids = get_user_ids()
    sent = 0
    failed = 0

    status = bot.send_message(
        message.chat.id,
        "📨 Xabar yuborish boshlandi..."
    )

    for user_id in user_ids:
        try:
            bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id
            )
            sent += 1
        except Exception:
            failed += 1

        time.sleep(0.05)

    bot.edit_message_text(
        f"📨 Yuborish yakunlandi.\n\n"
        f"✅ Yetkazildi: {sent}\n"
        f"❌ Xatolik: {failed}",
        chat_id=status.chat.id,
        message_id=status.message_id
    )


# =========================
# INSTAGRAM YUKLASH
# =========================

def download_instagram(url, folder):
    options = {
        "outtmpl": os.path.join(folder, "%(title).80s_%(id)s.%(ext)s"),
        "format": "best",
        "noplaylist": False,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 25,
        "max_filesize": 49 * 1024 * 1024,
    }

    with YoutubeDL(options) as ydl:
        ydl.download([url])

    files = []

    for path in Path(folder).iterdir():
        if not path.is_file():
            continue

        if path.suffix.lower() in (
            ".mp4", ".m4v", ".mov",
            ".jpg", ".jpeg", ".png", ".webp"
        ):
            if path.stat().st_size > 0:
                files.append(path)

    return sorted(files)


def send_downloaded_files(chat_id, files):
    if not files:
        return False

    for path in files:
        try:
            size = path.stat().st_size

            if size > 49 * 1024 * 1024:
                bot.send_message(
                    chat_id,
                    f"⚠️ Fayl juda katta: {path.name}"
                )
                continue

            suffix = path.suffix.lower()

            with open(path, "rb") as media:
                if suffix in (".jpg", ".jpeg", ".png", ".webp"):
                    bot.send_photo(chat_id, media)
                elif suffix in (".mp4", ".m4v", ".mov"):
                    bot.send_video(
                        chat_id,
                        media,
                        supports_streaming=True
                    )

            time.sleep(0.3)

        except Exception:
            logger.exception("Faylni Telegramga yuborishda xato.")
            bot.send_message(
                chat_id,
                f"⚠️ {path.name} faylini yuborib bo'lmadi."
            )

    return True


def handle_instagram_link(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    url = message.text.strip()

    if not is_admin(user_id):
        subscribed = check_subscription(user_id)

        if subscribed is not True:
            if subscribed is False:
                bot.send_message(
                    chat_id,
                    "Davom etish uchun kanalga obuna bo'ling.",
                    reply_markup=subscription_keyboard()
                )
            else:
                bot.send_message(
                    chat_id,
                    "Obunani tekshirishda xato. Keyinroq urinib ko'ring."
                )
            return

    status = bot.send_message(
        chat_id,
        "⏳ Havola tekshirilmoqda, kuting..."
    )

    try:
        with tempfile.TemporaryDirectory(prefix="instagram_") as folder:
            files = download_instagram(url, folder)

            if not files:
                bot.edit_message_text(
                    "❌ Media yuklab olinmadi.\n\n"
                    "Sabablari:\n"
                    "• Post yopiq bo'lishi mumkin.\n"
                    "• Instagram login talab qilishi mumkin.\n"
                    "• Havola noto'g'ri yoki media mavjud emas.\n"
                    "• Instagram yuklab olishni cheklagan bo'lishi mumkin.\n\n"
                    "Boshqa ochiq post havolasini sinab ko'ring.",
                    chat_id=chat_id,
                    message_id=status.message_id
                )
                return

            bot.edit_message_text(
                f"✅ {len(files)} ta fayl topildi. Yuborilmoqda...",
                chat_id=chat_id,
                message_id=status.message_id
            )

            send_downloaded_files(chat_id, files)

    except Exception:
        logger.exception("Instagram havolasini qayta ishlashda xato.")
        try:
            bot.edit_message_text(
                "❌ Yuklashda xatolik yuz berdi.\n"
                "Boshqa havolani sinab ko'ring.",
                chat_id=chat_id,
                message_id=status.message_id
            )
        except Exception:
            logger.exception("Xatolik xabarini yuborib bo'lmadi.")


# =========================
# MATN XABARLARI
# =========================

@bot.message_handler(content_types=["text"])
def text_handler(message):
    save_user(message.from_user)

    if is_admin(message.from_user.id):
        if message.from_user.id in broadcast_waiting:
            broadcast_waiting.discard(message.from_user.id)
            broadcast_message(message)
            return

    text_value = (message.text or "").strip()

    if is_instagram_url(text_value):
        handle_instagram_link(message)
    else:
        bot.reply_to(
            message,
            "Instagram video, Reels, rasm yoki carousel havolasini yuboring."
        )


# =========================
# WEBHOOK
# =========================

@app.get("/")
def home():
    return "Instagram bot is running.", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )
        if received_secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mos kelmadi.")
            abort(403)

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        logger.warning("Webhook bo'sh yoki noto'g'ri JSON oldi.")
        return "Bad Request", 400

    try:
        update = types.Update.de_json(json.dumps(data))
        logger.info(
            "Telegram update qabul qilindi: update_id=%s",
            data.get("update_id")
        )
        bot.process_new_updates([update])
        return "OK", 200

    except Exception:
        logger.exception("Webhook ichida xatolik yuz berdi.")
        return "Internal Server Error", 500


# =========================
# ISHGA TUSHIRISH
# =========================

setup_commands()

if RENDER_URL:
    try:
        webhook_url = RENDER_URL + WEBHOOK_PATH
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message", "callback_query"]
        )

        if result:
            logger.info("Webhook o'rnatildi: %s", webhook_url)
        else:
            logger.error("Telegram webhookni o'rnatmadi.")

    except Exception:
        logger.exception("Webhookni o'rnatishda xato.")
else:
    logger.error(
        "RENDER_EXTERNAL_URL yo'q. Webhook avtomatik o'rnatilmadi."
    )
