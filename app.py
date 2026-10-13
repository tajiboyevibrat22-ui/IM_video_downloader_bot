
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
COOKIES_FILE = os.environ.get("INSTAGRAM_COOKIES", "")

MAX_FILE_SIZE = 49 * 1024 * 1024
MAX_MEDIA = 10

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
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".webm"}


# DATABASE
def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "CREATE TABLE IF NOT EXISTS users "
            "(user_id INTEGER PRIMARY KEY)"
        )


def save_user(user_id):
    with db_lock, sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR IGNORE INTO users(user_id) VALUES (?)",
            (user_id,)
        )


def get_users():
    with db_lock, sqlite3.connect(DB_PATH) as con:
        return [
            row[0]
            for row in con.execute(
                "SELECT user_id FROM users"
            ).fetchall()
        ]


init_db()


# OBUNA TEKSHIRUVI
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


@bot.message_handler(commands=["start", "help"])
def start(message):
    save_user(message.from_user.id)

    if not is_subscribed(message.from_user.id):
        bot.send_message(
            message.chat.id,
            "Botdan foydalanish uchun avval kanalga obuna bo‘ling.",
            reply_markup=sub_keyboard()
        )
        return

    bot.send_message(
        message.chat.id,
        "Salom! 👋\n\n"
        "Instagram video, Reels, rasm, carousel va ochiq "
        "Story havolasini yuboring.\n\n"
        "Instagram cheklovlari sabab ayrim havolalar "
        "yuklanmasligi mumkin."
    )


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(
            call.id, "Obuna tasdiqlandi!"
        )
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


# ADMIN PANELI
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    save_user(message.from_user.id)

    if not is_admin(message):
        bot.reply_to(message, "⛔ Faqat admin uchun.")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "📢 Reklama yuborish",
        callback_data="ad_start"
    ))
    kb.add(types.InlineKeyboardButton(
        "👥 Foydalanuvchilar soni",
        callback_data="user_count"
    ))

    bot.send_message(
        message.chat.id,
        "🔐 Admin panel",
        reply_markup=kb
    )


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
        admin_state[ADMIN_ID] = {"state": "waiting_ad"}
        bot.send_message(
            call.message.chat.id,
            "Reklama xabarini yuboring "
            "(matn, rasm yoki video).\n"
            "Bekor qilish: /cancel"
        )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    if is_admin(message):
        admin_state.pop(ADMIN_ID, None)
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
        bot.answer_callback_query(
            call.id, "Reklama topilmadi."
        )
        return

    if call.data == "ad_cancel":
        admin_state.pop(ADMIN_ID, None)
        bot.answer_callback_query(call.id, "Bekor qilindi.")
        bot.send_message(
            call.message.chat.id,
            "Reklama yuborilmadi."
        )
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
            bot.copy_message(
                user_id, source_chat, source_message
            )
            sent += 1
        except Exception:
            failed += 1
            log.warning("Reklama yuborilmadi: %s", user_id)

        threading.Event().wait(0.05)

    bot.send_message(
        call.message.chat.id,
        f"✅ Yuborildi: {sent}\n❌ Yetkazilmadi: {failed}"
    )


# MEDIA FAYLLARINI TOPISH
def find_media(folder):
    found = []

    for root, _, names in os.walk(folder):
        for name in names:
            path = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()

            if (
                ext in MEDIA_EXTENSIONS
                and os.path.isfile(path)
                and os.path.getsize(path) > 0
            ):
                found.append(path)

    return sorted(found)


# YT-DLP ORQALI YUKLASH
def download_ytdlp(url, folder):
    os.makedirs(folder, exist_ok=True)

    opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(
            folder, "%(autonumber)03d_%(id)s.%(ext)s"
        ),
        "noplaylist": False,
        "max_filesize": MAX_FILE_SIZE,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 25,
        "retries": 2,
        "ignoreerrors": True,
        "writethumbnail": False,
    }

    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)

    return find_media(folder)


# GALLERY-DL ORQALI RASM/CAROUSEL YUKLASH
def download_gallery(url, folder):
    os.makedirs(folder, exist_ok=True)

    cmd = [
        sys.executable,
        "-m", "gallery_dl",
        "--dest", folder,
        "--retries", "2",
        "--http-timeout", "30",
    ]

    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        cmd.extend(["--cookies", COOKIES_FILE])

    cmd.append(url)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120
    )

    log.info("gallery-dl exit code: %s", result.returncode)

    if result.stdout:
        log.info(
            "gallery-dl natijasi: %s",
            result.stdout[-3000:]
        )

    if result.stderr:
        log.warning(
            "gallery-dl xatosi: %s",
            result.stderr[-3000:]
        )

    return find_media(folder)


# MEDIA YUKLASH VA TELEGRAMGA YUBORISH
def download_and_send(chat_id, status_id, url):
    try:
        with download_lock, tempfile.TemporaryDirectory() as folder:
            paths = []

            try:
                paths = download_ytdlp(
                    url, os.path.join(folder, "yt-dlp")
                )
            except Exception:
                log.exception("yt-dlp yuklash xatosi")

            if not paths:
                try:
                    paths = download_gallery(
                        url, os.path.join(folder, "gallery-dl")
                    )
                except subprocess.TimeoutExpired:
                    log.warning("gallery-dl vaqt chegarasidan oshdi")
                except Exception:
                    log.exception("gallery-dl yuklash xatosi")

            if not paths:
                raise RuntimeError(
                    "Media topilmadi; yuklash loglarini tekshiring."
                )

            sent = 0
            too_large = 0

            for path in paths[:MAX_MEDIA]:
                size = os.path.getsize(path)
                ext = os.path.splitext(path)[1].lower()

                if size > MAX_FILE_SIZE:
                    too_large += 1
                    continue

                with open(path, "rb") as f:
                    if ext in IMAGE_EXTENSIONS:
                        if size <= 9 * 1024 * 1024:
                            bot.send_photo(
                                chat_id, f, timeout=90
                            )
                        else:
                            bot.send_document(
                                chat_id, f, timeout=90
                            )

                    elif ext in VIDEO_EXTENSIONS:
                        if ext in (".mp4", ".m4v"):
                            bot.send_video(
                                chat_id,
                                f,
                                supports_streaming=True,
                                timeout=90
                            )
                        else:
                            bot.send_document(
                                chat_id, f, timeout=90
                            )

                    sent += 1

            if sent == 0:
                bot.edit_message_text(
                    "❌ Media topilmadi yoki fayl hajmi juda katta.",
                    chat_id,
                    status_id
                )
                return

            bot.delete_message(chat_id, status_id)

            if too_large:
                bot.send_message(
                    chat_id,
                    f"⚠️ {too_large} ta fayl 49 MB dan katta "
                    "bo‘lgani uchun yuborilmadi."
                )

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
                "❌ Yuklab bo‘lmadi. Havola ochiqligini, "
                "Instagram cheklovlarini va cookies sozlamasini tekshiring.",
                chat_id,
                status_id
            )
        except Exception:
            log.exception("Xato xabarini yuborib bo‘lmadi")


# FOYDALANUVCHI XABARLARI
@bot.message_handler(
    content_types=[
        "text", "photo", "video", "document",
        "animation", "audio", "voice"
    ]
)
def handle_message(message):
    save_user(message.from_user.id)

    state = admin_state.get(ADMIN_ID) if is_admin(message) else None

    if (
        is_admin(message)
        and isinstance(state, dict)
        and state.get("state") == "waiting_ad"
    ):
        admin_state[ADMIN_ID] = {
            "state": "confirm",
            "chat_id": message.chat.id,
            "message_id": message.message_id
        }

        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            "✅ Hammaga yuborish",
            callback_data="ad_confirm"
        ))
        kb.add(types.InlineKeyboardButton(
            "❌ Bekor qilish",
            callback_data="ad_cancel"
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
        url,
        re.IGNORECASE
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


# WEBHOOK
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
    if bot.set_webhook(
        url=RENDER_URL + "/telegram-webhook",
        secret_token=WEBHOOK_SECRET
    ):
        log.info("Webhook sozlandi.")
    else:
        log.error("Webhook sozlanmadi.")
else:
    log.error("RENDER_EXTERNAL_URL topilmadi.")
