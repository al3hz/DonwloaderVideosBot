import os
import asyncio
import threading
import tempfile
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

app = Flask(__name__)
bot_loop = asyncio.new_event_loop()

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 Envíame un enlace de:\n"
        "• TikTok (sin marca de agua)\n"
        "• YouTube / YouTube Shorts\n"
        "• Twitter / X\n"
        "• Instagram Reels\n"
        "• Facebook\n"
        "• Y muchas más...\n\n"
        "⚠️ Límite: 50 MB por video (límite de Telegram para bots)."
    )

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        ydl_opts = {
            "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
            "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
        }

        loop = asyncio.get_running_loop()

        def download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                title = info.get("title", "Video")
                duration = info.get("duration", 0)
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for f in os.listdir(tempfile.gettempdir()):
                        if f.startswith(os.path.basename(base)):
                            filename = os.path.join(tempfile.gettempdir(), f)
                            break
                return filename, title, duration

        filename, title, duration = await loop.run_in_executor(None, download)

        file_size = os.path.getsize(filename)

        if file_size > 50 * 1024 * 1024:
            await processing_msg.edit_text(
                "❌ El video pesa más de 50 MB.\n"
                "Telegram no permite enviar videos tan grandes a través de bots normales."
            )
            os.remove(filename)
            return

        await processing_msg.edit_text("📤 Subiendo video a Telegram...")

        with open(filename, "rb") as video_file:
            await update.message.reply_video(
                video=video_file,
                caption=f"✅ {title}",
                duration=duration if duration else None,
                supports_streaming=True,
            )

        os.remove(filename)

    except yt_dlp.utils.DownloadError as e:
        await processing_msg.edit_text(f"❌ Error de descarga:\n`{str(e)}`", parse_mode="Markdown")
    except Exception as e:
        await processing_msg.edit_text(f"❌ Error inesperado:\n`{str(e)}`", parse_mode="Markdown")
    else:
        await processing_msg.delete()

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

async def initialize_bot():
    await application.initialize()
    await application.start()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"https://{RENDER_EXTERNAL_URL}/webhook"
        await application.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook configurado en {webhook_url}")
    else:
        logging.warning("RENDER_EXTERNAL_HOSTNAME no está configurado; webhook no establecido.")

def run_bot_loop():
    asyncio.set_event_loop(bot_loop)
    bot_loop.run_forever()

threading.Thread(target=run_bot_loop, daemon=True).start()

asyncio.run_coroutine_threadsafe(initialize_bot(), bot_loop)

@app.route("/webhook", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
    return "OK", 200

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
