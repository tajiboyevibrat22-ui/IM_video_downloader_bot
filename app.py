
import os
import re
import tempfile
from flask import Flask, request, abort
import telebot
import yt_dlp

bot = telebot.TeleBot(BOT_TOKEN, threaded=False)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL")

if not BOT_TOKEN or not WEBHOOK_SECRET:
    raise RuntimeError("BOT_TOKEN va WEBHOOK_SECRET kerak")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(message, "Salom! 👋 Bot ishlayapti.")

@bot.message_handler(content_types=["text"])
def get_video(message):
    url = message.text.strip()

    if not re.match(
        r"https?://(www\.)?(instagram\.com|instagr\.am)/",
        url,
        re.I
    ):
        bot.reply_to(message, "Instagram havolasini yuboring.")
        return

    status = bot.reply_to(message, "⏳ Video yuklanmoqda...")

    try:
        with tempfile.TemporaryDirectory() as folder:
            options = {
                "format": "best[ext=mp4]/best",
                "outtmpl": os.path.join(folder, "video.%(ext)s"),
                "noplaylist": True,
                "max_filesize": 49 * 1024 * 1024,
                "quiet": True,
                "no_warnings": True,
            }

            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([url])

            files = os.listdir(folder)
            if not files:
                raise RuntimeError("Video topilmadi")

            path = os.path.join(folder, files[0])

            if os.path.getsize(path) > 49 * 1024 * 1024:
                bot.edit_message_text(
                    "Video hajmi juda katta (49 MB dan oshdi).",
                    message.chat.id,
                    status.message_id
                )
                return

            with open(path, "rb") as video:
                bot.send_video(message.chat.id, video)

            bot.delete_message(message.chat.id, status.message_id)

    except Exception:
        bot.edit_message_text(
            "❌ Yuklab bo‘lmadi. Havolani tekshiring yoki boshqa videoni sinab ko‘ring.",
            message.chat.id,
            status.message_id
        )

@app.get("/")
def home():
    return "Instagram bot ishlayapti!", 200

@app.post("/telegram-webhook")
def webhook():
    if request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token"
    ) != WEBHOOK_SECRET:
        abort(403)

    update = telebot.types.Update.de_json(
        request.get_json(force=True)
    )
    bot.process_new_updates([update])
    return "OK", 200

if RENDER_URL:
    bot.set_webhook(
        url=RENDER_URL.rstrip("/") + "/telegram-webhook",
        secret_token=WEBHOOK_SECRET
    )
