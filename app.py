
import os
import re
import sys
import sqlite3
import tempfile
import threading
import logging
import subprocess

from flask import Flask, request, abort
import telebot
from telebot import types
import yt_dlp

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL = "@Yangiyil_27"
DB_PATH = os.environ.get("DB_PATH", "/tmp/instagram_bot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
db_lock = threading.Lock()
download_lock = threading.Semaphore(2)
admin_state = {}

MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".mp4", ".m4v", ".mov", ".webm"
}


# ---------- DATABASE ----------

def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)


def save_user(user_id):
    with db_lock, sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES (?)",
            (user_id,)
        )


def get_users():
    with db_lock, sqlite3.connect(DB_PATH) as con:
        return [
            row[0] for row in con.execute(
                "SELECT user_id FROM users"
            ).fetchall()
        ]


init_db()


# ---------- SUBSCRIPTION ----------

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in (
            "creator", "administrator", "member"
        )
    except Exception:
        log.exception("Obuna tekshiruvi xatosi")
        return False


def sub_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "📢 Kanalga obuna bo‘lish",
        url="https://t.me/Yangiyil_27"
    ))
    kb.add(types.InlineKeyboardButton(
        "✅ Obunani tekshirish",
        callback_data="check_sub"
    ))
    return kb


def is_admin(message):
    return (
        ADMIN_ID != 0
        and message.from_user.id == ADMIN_ID
    )


# ---------- START ----------

@bot.message_handler(commands=["start", "help"])
def start(message):
    save_user(message.from_user.id)

    if not is_subscribed(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Botdan foydalanish uchun avval "
            "kanalga obuna bo‘ling.",
            reply_markup=sub_keyboard()
        )
        return

    bot.send_message(
        message.chat.id,
        "Salom! 👋\n\n"
        "Instagram video, Reels, rasm va carousel "
        "havolasini yuboring.\n\n"
        "Eslatma: ayrim yopiq yoki login talab qiladigan "
        "postlar yuklanmasligi mumkin."
    )


@bot.callback_query_handler(
    func=lambda c: c.data == "check_sub"
)
def check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi!")
        bot.send_message(
            call.message.chat.id,
            "Rahmat! Instagram havolasini yuboring."
        )
    else:
        bot.answer_callback_query(
            call.id,
            "Obuna aniqlanmadi.",
            show_alert=True
        )


# ---------- ADMIN ----------

@bot.message_handler(commands=["admin"])
def admin_panel(message):
    save_user(message.from_user.id)

    if not is_admin(message):
        bot.reply_to(message, "⛔ Faqat admin uchun.")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "📢 Reklama yuborish", callback_data="ad_start"
    ))
    kb.add(types.InlineKeyboardButton(
        "👥 Foydalanuvchilar soni", callback_data="user_count"
    ))
    bot.send_message(message.chat.id, "🔐 Admin panel", reply_markup=kb)


@bot.callback_query_handler(
    func=lambda c: c.data in ("ad_start", "user_count")
)
def admin_actions(call):
    if ADMIN_ID == 0 or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "Ruxsat yo‘q.", show_alert=True
        )
        return

    if call.data == "user_count":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"👥 Foydalanuvchilar: {len(get_users())} ta"
        )
    else:
        bot.answer_callback_query(call.id)
        admin_state[call.from_user.id] = "waiting_ad"
        bot.send_message(
            call.message.chat.id,
            "Reklama xabarini yuboring.\n"
            "Matn, rasm yoki video bo‘lishi mumkin.\n"
            "Bekor qilish: /cancel"
        )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    if is_admin(message):
        admin_state.pop(message.from_user.id, None)
        bot.reply_to(message, "Bekor qilindi.")


@bot.callback_query_handler(
    func=lambda c: c.data in ("ad_confirm", "ad_cancel")
)
def confirm_broadcast(call):
    if ADMIN_ID == 0 or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "Ruxsat yo‘q.", show_alert=True
        )
        return

    pending = admin_state.get(ADMIN_ID)

    if not isinstance(pending, dict) or pending.get("state") != "confirm":
        bot.answer_callback_query(call.id, "Reklama topilmadi.")
        return

    if call.data == "ad_cancel":
        admin_state.pop(ADMIN_ID, None)
        bot.answer_callback_query(call.id, "Bekor qilindi.")
        bot.send_message(call.message.chat.id, "Reklama yuborilmadi.")
        return

    source_chat = pending["chat_id"]
    source_message = pending["message_id"]
    admin_state.pop(ADMIN_ID, None)

    bot.answer_callback_query(call.id, "Yuborish boshlandi.")
    users = get_users()
    sent = 0
    failed = 0

    bot.send_message(
        call.message.chat.id,
        f"📤 {len(users)} ta foydalanuvchiga yuborilmoqda."
    )

    for user_id in users:
        try:
            bot.copy_message(user_id, source_chat, source_message)
            sent += 1
        except Exception:
            failed += 1
            log.warning("Reklama yuborilmadi: %s", user_id)

        threading.Event().wait(0.05)

    bot.send_message(
        call.message.chat.id,
        f"✅ Yuborildi: {sent}\n❌ Yetkazilmadi: {failed}"
    )


# ---------- MEDIA HELPERS ----------

def find_media(folder):
    paths = []

    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()

            if (
                ext in MEDIA_EXTENSIONS
                and os.path.isfile(path)
                and os.path.getsize(path) > 0
            ):
                paths.append(path)

    return sorted(paths)


def download_ytdlp(url, folder):
    os.makedirs(folder, exist_ok=True)

    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(
            folder, "%(autonumber)03d_%(id)s.%(ext)s"
        ),
        "noplaylist": False,
        "max_filesize": 49 * 1024 * 1024,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 20,
        "retries": 1,
        "ignoreerrors": False,
    }

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.extract_info(url, download=True)

    return find_media(folder)


def download_gallery(url, folder):
    os.makedirs(folder, exist_ok=True)

    result = subprocess.run(
        [
            sys.executable, "-m", "gallery_dl",
            "--dest", folder,
            "--retries", "1",
            "--http-timeout", "15",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=60
    )

    log.info("gallery-dl exit code: %s", result.returncode)
    if result.returncode != 0:
        log.warning(
            "gallery-dl xatosi: %s",
            (result.stderr or result.stdout or "")[-2000:]
        )

    return find_media(folder)


# ---------- DOWNLOAD AND SEND ----------

def download_and_send(chat_id, status_id, url):
    try:
        # Bir vaqtda ko‘pi bilan 2 ta yuklash
        with download_lock:
            with tempfile.TemporaryDirectory() as folder:
                paths = []

                # Birinchi navbatda yt-dlp:
                # Reels va videolar uchun asosiy usul.
                try:
                    paths = download_ytdlp(
                        url, os.path.join(folder, "video")
                    )
                except Exception:
                    log.exception("yt-dlp yuklash xatosi")

                # Video usuli ishlamasa, gallery-dl:
                # rasm va carousel uchun zaxira usul.
                if not paths:
                    try:
                        paths = download_gallery(
                            url, os.path.join(folder, "gallery")
                        )
                    except subprocess.TimeoutExpired:
                        log.warning("gallery-dl vaqti tugadi")
                    except Exception:
                        log.exception("gallery-dl yuklash xatosi")

                if not paths:
                    raise RuntimeError("Hech qanday media topilmadi.")

                sent = 0
                too_large = 0

                for path in paths[:10]:
                    size = os.path.getsize(path)
                    ext = os.path.splitext(path)[1].lower()

                    if size > 49 * 1024 * 1024:
                        too_large += 1
                        continue

                    with open(path, "rb") as media:
                        if ext in (".jpg", ".jpeg", ".png", ".webp"):
                            if size <= 9 * 1024 * 1024:
                                bot.send_photo(chat_id, media)
                            else:
                                bot.send_document(
                                    chat_id, media, timeout=90
                                )

                        elif ext in (".mp4", ".m4v", ".mov", ".webm"):
                            bot.send_video(
                                chat_id,
                                media,
                                supports_streaming=True,
                                timeout=90
                            )

                    sent += 1

                if sent == 0:
                    bot.edit_message_text(
                        "❌ Media topilmadi yoki fayl hajmi juda katta.",
                        chat_id, status_id
                    )
                    return

                bot.delete_message(chat_id, status_id)

                ad_text = os.environ.get("AD_TEXT", "").strip()
                ad_url = os.environ.get("AD_URL", "").strip()

                if ad_text and ad_url:
                    kb = types.InlineKeyboardMarkup()
                    kb.add(types.InlineKeyboardButton(
                        "Reklamani ko‘rish", url=ad_url
                    ))
                    bot.send_message(
                        chat_id, ad_text, reply_markup=kb
                    )

    except Exception:
        log.exception("Media yuklash/yuborish xatosi")
        try:
            bot.edit_message_text(
                "❌ Yuklab bo‘lmadi.\n\n"
                "Havola ochiq ekanini tekshiring. Instagram "
                "cheklovlari yoki login talabi sababli ayrim "
                "postlar yuklanmasligi mumkin.",
                chat_id, status_id
            )
        except Exception:
            log.exception("Xato xabarini yuborib bo‘lmadi")


# ---------- MESSAGES ----------

@bot.message_handler(
    content_types=[
        "text", "photo", "video", "document",
        "animation", "audio", "voice"
    ]
)
def handle_message(message):
    save_user(message.from_user.id)

    if (
        is_admin(message)
        and admin_state.get(message.from_user.id) == "waiting_ad"
    ):
        admin_state[message.from_user.id] = {
            "state": "confirm",
            "chat_id": message.chat.id,
            "message_id": message.message_id
        }

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "✅ Hammaga yuborish", callback_data="ad_confirm"
        ))
        kb.add(types.InlineKeyboardButton(
            "❌ Bekor qilish", callback_data="ad_cancel"
        ))
        bot.send_message(
            message.chat.id,
            "Reklama tayyor. Hammaga yuborilsin?",
            reply_markup=kb
        )
        return

    if message.content_type != "text" or not message.text:
        return

    url = message.text.strip()

    if not re.match(
        r"^https?://(www\.)?(instagram\.com|instagr\.am)/",
        url, re.IGNORECASE
    ):
        bot.reply_to(message, "❗ Instagram havolasini yuboring.")
        return

    if not is_subscribed(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Davom etish uchun kanalga obuna bo‘ling.",
            reply_markup=sub_keyboard()
        )
        return

    status = bot.reply_to(message, "⏳ Media yuklanmoqda...")

    threading.Thread(
        target=download_and_send,
        args=(message.chat.id, status.message_id, url),
        daemon=True
    ).start()


# ---------- WEBHOOK ----------

@app.get("/")
def home():
    return "Instagram bot ishlayapti!", 200


@app.get("/healthz")
def health():
    return "OK", 200


@app.post("/telegram-webhook")
def webhook():
    if (
        request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        != WEBHOOK_SECRET
    ):
        abort(403)

    try:
        update = telebot.types.Update.de_json(
            request.get_data(as_text=True)
        )
        bot.process_new_updates([update])
        return "OK", 200
    except Exception:
        log.exception("Webhook xatosi")
        return "Webhook error", 500


if RENDER_URL:
    result = bot.set_webhook(
        url=RENDER_URL + "/telegram-webhook",
        secret_token=WEBHOOK_SECRET
    )
    if result:
        log.info("Webhook sozlandi.")
    else:
        log.error("Webhook sozlanmadi.")
else:
    log.error("RENDER_EXTERNAL_URL topilmadi.")
