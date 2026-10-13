
import os
import re
import sys
import glob
import time
import sqlite3
import logging
import tempfile
import threading
import subprocess
from urllib.parse import urlparse

from flask import Flask, request, abort
import telebot
from telebot import types
from yt_dlp import YoutubeDL

# ========== SOZLAMALAR ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
CHANNEL = "@Yangiyil_27"
DB_PATH = os.getenv("DB_PATH", "/tmp/instagram_bot.db")

if not BOT_TOKEN or not WEBHOOK_SECRET:
    raise RuntimeError("BOT_TOKEN va WEBHOOK_SECRET sozlanishi kerak.")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("instagram-bot")

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)

db_lock = threading.Lock()
job_lock = threading.Lock()
busy_users = set()
broadcast_waiting = set()


# ========== BAZA ==========
def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA busy_timeout=20000")
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at INTEGER NOT NULL
            )
        """)


def save_user(user_id):
    with db_lock:
        with db_connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id, joined_at) VALUES (?, ?)",
                (user_id, int(time.time()))
            )


def get_users():
    with db_lock:
        with db_connect() as conn:
            return [
                row[0] for row in
                conn.execute("SELECT user_id FROM users").fetchall()
            ]


init_db()


# ========== OBUNA ==========
def is_admin(user_id):
    return ADMIN_ID != 0 and user_id == ADMIN_ID


def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member")
    except Exception:
        log.exception("Obuna tekshirish xatosi")
        return False


def show_subscription(chat_id):
    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(
        "📢 Kanalga obuna bo‘lish",
        url="https://t.me/Yangiyil_27"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "✅ Obunani tekshirish",
        callback_data="check_sub"
    ))
    bot.send_message(
        chat_id,
        "🔒 Botdan foydalanish uchun kanalga obuna bo‘ling.",
        reply_markup=keyboard
    )


def check_access(message):
    user_id = message.from_user.id
    save_user(user_id)

    if is_admin(user_id):
        return True

    if not is_subscribed(user_id):
        show_subscription(message.chat.id)
        return False

    return True


# ========== INSTAGRAM URL ==========
def extract_instagram_url(text):
    if not text:
        return None

    match = re.search(
        r"https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s]+",
        text,
        re.IGNORECASE
    )
    if not match:
        return None

    url = match.group(0).rstrip(".,!?)]}")
    host = (urlparse(url).hostname or "").lower()

    if host not in (
        "instagram.com", "www.instagram.com",
        "instagr.am", "www.instagr.am"
    ):
        return None

    return url


# ========== MEDIA YUKLASH ==========
def find_media(folder):
    extensions = (
        "*.mp4", "*.m4v", "*.mov", "*.webm",
        "*.jpg", "*.jpeg", "*.png", "*.webp"
    )
    found = []

    for ext in extensions:
        found.extend(
            glob.glob(os.path.join(folder, "**", ext), recursive=True)
        )

    valid = [
        p for p in found
        if os.path.isfile(p)
        and os.path.getsize(p) < 49 * 1024 * 1024
    ]
    return sorted(set(valid))[:10]


def run_ytdlp(url, folder):
    options = {
        "outtmpl": os.path.join(folder, "%(id)s_%(autonumber)s.%(ext)s"),
        "format": "best[ext=mp4]/best",
        "noplaylist": False,
        "ignoreerrors": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": 49 * 1024 * 1024,
        "restrictfilenames": True,
    }

    with YoutubeDL(options) as ydl:
        ydl.download([url])


def run_gallery_dl(url, folder):
    result = subprocess.run(
        [
            sys.executable, "-m", "gallery_dl",
            "--directory", folder,
            "--retries", "2",
            "--http-timeout", "30",
            url
        ],
        capture_output=True,
        text=True,
        timeout=120
    )

    if result.stdout:
        log.info("gallery-dl: %s", result.stdout[-1000:])
    if result.stderr:
        log.info("gallery-dl xabari: %s", result.stderr[-1000:])


def download_media(url, folder):
    try:
        run_ytdlp(url, folder)
    except Exception:
        log.exception("yt-dlp xatosi")

    files = find_media(folder)
    if files:
        return files

    try:
        run_gallery_dl(url, folder)
    except Exception:
        log.exception("gallery-dl xatosi")

    return find_media(folder)


def send_media(chat_id, files):
    sent = 0

    for path in files:
        ext = os.path.splitext(path)[1].lower()

        try:
            with open(path, "rb") as media:
                if ext in (".jpg", ".jpeg", ".png", ".webp"):
                    bot.send_photo(chat_id, media)
                elif ext == ".mp4":
                    bot.send_video(
                        chat_id, media, supports_streaming=True
                    )
                else:
                    bot.send_document(chat_id, media)
            sent += 1
        except Exception:
            log.exception("Fayl yuborilmadi: %s", path)

    return sent


def download_job(chat_id, user_id, url):
    with job_lock:
        if user_id in busy_users:
            bot.send_message(chat_id, "⏳ Avvalgi yuklash tugashini kuting.")
            return
        busy_users.add(user_id)

    status_message = None

    try:
        status_message = bot.send_message(chat_id, "⏳ Yuklanmoqda...")

        with tempfile.TemporaryDirectory(prefix="ig_") as folder:
            files = download_media(url, folder)

            try:
                bot.delete_message(chat_id, status_message.message_id)
            except Exception:
                pass

            if not files:
                bot.send_message(
                    chat_id,
                    "❌ Yuklab bo‘lmadi.\n\n"
                    "Ayrim yopiq yoki login talab qiladigan postlar "
                    "yuklanmasligi mumkin. Ochiq Reel yoki post havolasini "
                    "qayta yuboring."
                )
                return

            bot.send_message(chat_id, "📤 Fayllar yuborilmoqda...")
            sent = send_media(chat_id, files)

            if sent:
                bot.send_message(chat_id, f"✅ {sent} ta fayl yuborildi.")
            else:
                bot.send_message(
                    chat_id,
                    "❌ Fayllar topildi, ammo Telegramga yuborilmadi."
                )

    except Exception:
        log.exception("Yuklash jarayoni xatosi")
        bot.send_message(chat_id, "❌ Xatolik yuz berdi. Keyinroq urinib ko‘ring.")
    finally:
        with job_lock:
            busy_users.discard(user_id)


# ========== START ==========
@bot.message_handler(commands=["start"])
def start(message):
    if not check_access(message):
        return

    if is_admin(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Salom, admin! 👋\n\n"
            "Instagram video, Reels, rasm va carousel havolasini yuboring.\n\n"
            "/admin — admin panel\n"
            "/help — yordam"
        )
    else:
        bot.send_message(
            message.chat.id,
            "Salom! 👋\n\n"
            "Instagram video, Reels, rasm va carousel havolasini yuboring.\n\n"
            "Eslatma: ayrim yopiq yoki login talab qiladigan postlar "
            "yuklanmasligi mumkin."
        )


# ========== HELP: FAQAT ADMIN ==========
@bot.message_handler(commands=["help"])
def help_command(message):
    if not is_admin(message.from_user.id):
        return

    bot.send_message(
        message.chat.id,
        "⚙️ Admin yordam\n\n"
        "/admin — admin panel\n"
        "/help — yordam"
    )


# ========== ADMIN PANEL ==========
@bot.message_handler(commands=["admin"])
def admin_command(message):
    if not is_admin(message.from_user.id):
        return

    keyboard = types.InlineKeyboardMarkup()
    keyboard.add(types.InlineKeyboardButton(
        "👥 Foydalanuvchilar soni",
        callback_data="admin_count"
    ))
    keyboard.add(types.InlineKeyboardButton(
        "📣 Xabar yuborish",
        callback_data="admin_broadcast"
    ))

    bot.send_message(
        message.chat.id,
        "⚙️ Admin panel",
        reply_markup=keyboard
    )


@bot.message_handler(commands=["cancel"])
def cancel_command(message):
    broadcast_waiting.discard(message.from_user.id)
    bot.send_message(message.chat.id, "Bekor qilindi.")


# ========== CALLBACKLAR ==========
@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def subscription_callback(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi!")
        bot.send_message(
            call.message.chat.id,
            "✅ Obuna tasdiqlandi. /start ni bosing."
        )
    else:
        bot.answer_callback_query(
            call.id,
            "Avval kanalga obuna bo‘ling.",
            show_alert=True
        )


@bot.callback_query_handler(
    func=lambda c: c.data in ("admin_count", "admin_broadcast")
)
def admin_callback(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.", show_alert=True)
        return

    if call.data == "admin_count":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"👥 Foydalanuvchilar: {len(get_users())}"
        )

    elif call.data == "admin_broadcast":
        broadcast_waiting.add(call.from_user.id)
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "📣 Yuboriladigan matnni jo‘nating.\nBekor qilish: /cancel"
        )


# ========== XABAR TARQATISH ==========
@bot.message_handler(
    content_types=["text"],
    func=lambda m: m.from_user is not None
    and m.from_user.id in broadcast_waiting
)
def broadcast_message(message):
    uid = message.from_user.id

    if not is_admin(uid):
        broadcast_waiting.discard(uid)
        return

    if (message.text or "").startswith("/cancel"):
        broadcast_waiting.discard(uid)
        bot.send_message(message.chat.id, "Bekor qilindi.")
        return

    broadcast_waiting.discard(uid)
    users = get_users()
    sent = 0
    failed = 0

    bot.send_message(message.chat.id, "⏳ Xabar yuborilmoqda...")

    for user_id in users:
        try:
            bot.send_message(user_id, message.text)
            sent += 1
        except Exception:
            failed += 1
        time.sleep(0.05)

    bot.send_message(
        message.chat.id,
        f"✅ Yakunlandi.\nYetkazildi: {sent}\nXatolik: {failed}"
    )


# ========== INSTAGRAM HAVOLALARI ==========
@bot.message_handler(content_types=["text"])
def text_handler(message):
    if not check_access(message):
        return

    url = extract_instagram_url(message.text)

    if not url:
        # Oddiy foydalanuvchiga /help yoki /admin taklif qilinmaydi
        if is_admin(message.from_user.id):
            bot.send_message(
                message.chat.id,
                "Instagram havolasini yuboring.\n/admin — admin panel"
            )
        return

    threading.Thread(
        target=download_job,
        args=(message.chat.id, message.from_user.id, url),
        daemon=True
    ).start()


# ========== RENDER WEBHOOK ==========
@app.get("/")
def home():
    return "Instagram bot ishlayapti.", 200


@app.get("/health")
def health():
    return {"status": "ok"}, 200


@app.post("/telegram-webhook")
def telegram_webhook():
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )
    if secret != WEBHOOK_SECRET:
        abort(403)

    update = telebot.types.Update.de_json(request.get_data(as_text=True))
    bot.process_new_updates([update])
    return "OK", 200


# Buyruqlar menyusi: oddiy foydalanuvchiga admin/help ko‘rinmaydi
try:
    bot.set_my_commands([
        types.BotCommand("start", "Botni boshlash")
    ])
    if ADMIN_ID:
        bot.set_my_commands(
            [
                types.BotCommand("start", "Botni boshlash"),
                types.BotCommand("admin", "Admin panel"),
                types.BotCommand("help", "Admin yordami"),
            ],
            scope=types.BotCommandScopeChat(ADMIN_ID)
        )
except Exception:
    log.exception("Buyruqlar menyusini sozlash xatosi")


if RENDER_URL:
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(
            url=f"{RENDER_URL}/telegram-webhook",
            secret_token=WEBHOOK_SECRET
        )
        log.info("Webhook sozlandi.")
    except Exception:
        log.exception("Webhook sozlashda xato")
