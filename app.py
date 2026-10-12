
import os
import re
import tempfile
import threading
import logging

from flask import Flask, request, abort
import telebot
import yt_dlp

# 1. Render Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

if not BOT_TOKEN:
    raise RuntimeError("Render Environment ichida BOT_TOKEN topilmadi.")

if not WEBHOOK_SECRET:
    raise RuntimeError("Render Environment ichida WEBHOOK_SECRET topilmadi.")

# 2. Bot va Flask ilovasi
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# 3. /start va /help buyruqlari
@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "Salom! 👋\n\n"
        "Instagram Reels yoki ochiq video havolasini yuboring.\n"
        "Men videoni yuklab, sizga jo‘natishga harakat qilaman."
    )

# 4. Instagram havolasini tekshirish va video yuklash
@bot.message_handler(content_types=["text"])
def get_video(message):
    url = message.text.strip()

    if not re.match(
        r"^https?://(www\.)?(instagram\.com|instagr\.am)/",
        url,
        re.IGNORECASE
    ):
        bot.reply_to(message, "❗ Iltimos, Instagram video havolasini yuboring.")
        return

    status = bot.reply_to(message, "⏳ Video yuklanmoqda. Biroz kuting...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            options = {
                "format": "best[ext=mp4]/best",
                "outtmpl": os.path.join(folder, "video.%(ext)s"),
                "noplaylist": True,
                "max_filesize": 49 * 1024 * 1024,
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
                "retries": 2,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])

            files = [
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, name))
            ]

            if not files:
                raise RuntimeError("Yuklangan video fayli topilmadi.")

            video_path = max(files, key=os.path.getsize)

            if os.path.getsize(video_path) > 49 * 1024 * 1024:
                bot.edit_message_text(
                    "❌ Video 49 MB dan katta. Kichikroq video yuboring.",
                    message.chat.id,
                    status.message_id
                )
                return

            with open(video_path, "rb") as video:
                bot.send_video(
                    message.chat.id,
                    video,
                    timeout=60,
                    supports_streaming=True
                )

            bot.delete_message(message.chat.id, status.message_id)

    except Exception:
        app.logger.exception("Video yuklashda xatolik")
        try:
            bot.edit_message_text(
                "❌ Videoni yuklab bo‘lmadi.\n\n"
                "Havolani tekshiring. Video yopiq, cheklangan "
                "yoki vaqtincha yuklab bo‘lmaydigan bo‘lishi mumkin.",
                message.chat.id,
                status.message_id
            )
        except Exception:
            pass

# 5. Render xizmatini tekshirish
@app.get("/")
def home():
    return "Instagram bot ishlayapti!", 200

@app.get("/healthz")
def health():
    return "OK", 200

# 6. Telegram webhook
@app.post("/telegram-webhook")
def webhook():
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        abort(403)

    try:
        
update = telebot.types.Update.de_json(
    request.get_data(as_text=True)
)
        # Telegramga tez javob berib, yangilanishni alohida ishlash
        threading.Thread(
            target=bot.process_new_updates,
            args=([update],),
            daemon=True
        ).start()

        return "OK", 200

    except Exception:
        app.logger.exception("Webhook xatosi")
        return "Webhook error", 500

# 7. Webhook manzilini sozlash
if RENDER_URL:
    webhook_url = RENDER_URL.rstrip("/") + "/telegram-webhook"
    result = bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET
    )

    if result:
        app.logger.info("Telegram webhook muvaffaqiyatli sozlandi.")
    else:
        app.logger.error("Telegram webhook sozlanmadi.")
else:
    app.logger.error("RENDER_EXTERNAL_URL topilmadi.")
