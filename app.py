
import os
import re
import sqlite3
import tempfile
import threading
import logging

from flask import Flask, request, abort
import telebot
from telebot import types
import yt_dlp

# Render Environment Variables
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
CHANNEL = "@Yangiyil_27"
DB_PATH = os.environ.get("DB_PATH", "/tmp/instagram_bot.db")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True)
app = Flask(__name__)
db_lock = threading.Lock()
admin_state = {}

# Foydalanuvchilar bazasi
def init_db():
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
            row[0] for row in
            con.execute("SELECT user_id FROM users").fetchall()
        ]

init_db()


def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(CHANNEL, user_id)
        return member.status in ("creator", "administrator", "member")
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
    return ADMIN_ID != 0 and message.from_user.id == ADMIN_ID


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
        "Instagram Reels, video, rasm yoki carousel havolasini yuboring.\n"
        "Story havolalari ham sinab ko‘riladi, ammo hammasi ham ochilmasligi mumkin."
    )


@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(call):
    if is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Obuna tasdiqlandi!")
        bot.send_message(
            call.message.chat.id,
            "Rahmat! Endi Instagram havolasini yuboring."
        )
    else:
        bot.answer_callback_query(
            call.id,
            "Obuna aniqlanmadi. Kanalga obuna bo‘ling.",
            show_alert=True
        )


# Admin panel
@bot.message_handler(commands=["admin"])
def admin_panel(message):
    save_user(message.from_user.id)

    if not is_admin(message):
        bot.reply_to(message, "⛔ Bu bo‘lim faqat admin uchun.")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "📢 Reklama yuborish", callback_data="ad_start"
    ))
    kb.add(types.InlineKeyboardButton(
        "👥 Foydalanuvchilar soni", callback_data="user_count"
    ))
    bot.send_message(message.chat.id, "🔐 Admin panel", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data in ("ad_start", "user_count"))
def admin_actions(call):
    if ADMIN_ID == 0 or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.", show_alert=True)
        return

    if call.data == "user_count":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"👥 Bazadagi foydalanuvchilar: {len(get_users())} ta"
        )
    else:
        bot.answer_callback_query(call.id)
        admin_state[call.from_user.id] = "waiting_ad"
        bot.send_message(
            call.message.chat.id,
            "Reklama sifatida yubormoqchi bo‘lgan xabaringni yubor.\n"
            "Matn, rasm, video yoki boshqa Telegram xabari bo‘lishi mumkin.\n\n"
            "Bekor qilish uchun /cancel yoz."
        )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    if is_admin(message):
        admin_state.pop(message.from_user.id, None)
        bot.reply_to(message, "Bekor qilindi.")


@bot.callback_query_handler(func=lambda c: c.data in ("ad_confirm", "ad_cancel"))
def confirm_broadcast(call):
    if ADMIN_ID == 0 or call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Ruxsat yo‘q.", show_alert=True)
        return

    pending = admin_state.get(ADMIN_ID)
    if not pending or pending.get("state") != "confirm":
        bot.answer_callback_query(call.id, "Reklama topilmadi.")
        return

    if call.data == "ad_cancel":
        admin_state.pop(ADMIN_ID, None)
        bot.answer_callback_query(call.id, "Bekor qilindi.")
        bot.send_message(call.message.chat.id, "Reklama yuborilmadi.")
        return

    source_chat, source_message = pending["chat_id"], pending["message_id"]
    admin_state.pop(ADMIN_ID, None)
    bot.answer_callback_query(call.id, "Yuborish boshlandi.")

    users = get_users()
    sent = 0
    failed = 0
    bot.send_message(call.message.chat.id, f"📤 {len(users)} ta foydalanuvchiga yuborish boshlandi.")

    for user_id in users:
        try:
            bot.copy_message(user_id, source_chat, source_message)
            sent += 1
        except Exception:
            failed += 1
            log.warning("Reklama yuborilmadi: %s", user_id)
        # Telegram limitlariga rioya qilish uchun kichik tanaffus
        threading.Event().wait(0.05)

    bot.send_message(
        call.message.chat.id,
        f"✅ Yuborildi: {sent}\n❌ Yetkazilmadi: {failed}"
    )


# Admin reklama xabarini yuborganda uni tasdiqlashga tayyorlash
@bot.message_handler(
    content_types=["text", "photo", "video", "document", "animation", "audio", "voice"]
)
def handle_message(message):
    save_user(message.from_user.id)

    if is_admin(message) and admin_state.get(message.from_user.id) == "waiting_ad":
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
            "Reklama xabari tayyor. Hammaga yuborilsinmi?",
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


def download_and_send(chat_id, status_id, url):
    try:
        with tempfile.TemporaryDirectory() as folder:
            options = {
                "format": "best[ext=mp4]/best",
                "outtmpl": os.path.join(folder, "%(autonumber)03d_%(id)s.%(ext)s"),
                "noplaylist": False,
                "max_filesize": 49 * 1024 * 1024,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 25,
                "retries": 2,
                "ignoreerrors": True,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.extract_info(url, download=True)

            paths = [
                os.path.join(folder, name)
                for name in sorted(os.listdir(folder))
                if os.path.isfile(os.path.join(folder, name))
                and os.path.getsize(os.path.join(folder, name)) > 0
            ]

            if not paths:
                raise RuntimeError("Media topilmadi.")

            sent = 0
            for path in paths[:10]:
                size = os.path.getsize(path)
                if size > 49 * 1024 * 1024:
                    continue

                ext = os.path.splitext(path)[1].lower()
                with open(path, "rb") as f:
                    if ext in (".jpg", ".jpeg", ".png", ".webp"):
                        if size <= 9 * 1024 * 1024:
                            bot.send_photo(chat_id, f)
                        else:
                            bot.send_document(chat_id, f)
                    elif ext in (".mp4", ".m4v", ".mov", ".webm"):
                        bot.send_video(
                            chat_id, f,
                            supports_streaming=True,
                            timeout=90
                        )
                    else:
                        bot.send_document(chat_id, f)
                sent += 1

            if sent == 0:
                bot.edit_message_text(
                    "❌ Yuborishga mos media topilmadi yoki fayl juda katta.",
                    chat_id, status_id
                )
            else:
                bot.delete_message(chat_id, status_id)

            # Reklama joyi: muvaffaqiyatli yuklashdan keyin ixtiyoriy reklama
            ad_text = os.environ.get("AD_TEXT", "").strip()
            ad_url = os.environ.get("AD_URL", "").strip()
            if ad_text and ad_url:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("Reklamani ko‘rish", url=ad_url))
                bot.send_message(chat_id, ad_text, reply_markup=kb)

    except Exception:
        log.exception("Media yuklash/yuborish xatosi")
        try:
            bot.edit_message_text(
                "❌ Yuklab bo‘lmadi. Havola ochiq ekanini tekshiring. "
                "Instagram ayrim Story yoki postlarni login bilan cheklashi mumkin.",
                chat_id, status_id
            )
        except Exception:
            pass


@app.get("/")
def home():
    return "Instagram bot ishlayapti!", 200


@app.get("/healthz")
def health():
    return "OK", 200


@app.post("/telegram-webhook")
def webhook():
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        abort(403)

    try:
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        threading.Thread(
            target=bot.process_new_updates,
            args=([update],),
            daemon=True
        ).start()
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
