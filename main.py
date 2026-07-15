import os
import re
import time
import asyncio
import threading
import tempfile
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import requests

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
app = Flask(__name__)

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 Envíame un enlace de:\n"
        "• TikTok (sin marca de agua)\n"
        "• YouTube / YouTube Shorts\n"
        "• Twitter / X\n"
        "• Instagram (Reels, fotos, videos, stories)\n"
        "• Facebook\n"
        "• Y muchas más...\n\n"
        "⚠️ Límite: 50 MB por archivo (límite de Telegram para bots)."
    )
    await update.message.reply_text(text)

def get_ydl_opts():
    return {
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }

def download_instagram_photo(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.5",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    html = resp.text

    image_url = None
    og = re.search(r'<meta\s+property="og:image"\s+content="([^"]+)"', html)
    if og:
        image_url = og.group(1)

    if not image_url:
        og = re.search(r'<meta\s+name="twitter:image"\s+content="([^"]+)"', html)

    if not image_url:
        m = re.search(r'<img[^>]+src="([^"]+)"[^>]*style="[^"]*object-fit[^"]*"', html)

    if not image_url:
        raise Exception("No se pudo extraer la imagen de Instagram.")

    if image_url.startswith("//"):
        image_url = "https:" + image_url

    img_resp = requests.get(image_url, headers=headers, timeout=60)
    img_resp.raise_for_status()

    ext = "jpg"
    ct = img_resp.headers.get("Content-Type", "")
    if "png" in ct:
        ext = "png"
    elif "webp" in ct:
        ext = "webp"

    filename = os.path.join(tempfile.gettempdir(), f"instagram_{int(time.time())}.{ext}")
    with open(filename, "wb") as f:
        f.write(img_resp.content)

    return filename, "Instagram Photo"

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        loop = asyncio.get_running_loop()
        is_instagram = "instagram.com" in url

        def download():
            if is_instagram:
                try:
                    fname, ftitle = download_instagram_photo(url)
                    return fname, ftitle, 0, False
                except Exception:
                    pass
            with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                title = info.get("title", "Video")
                duration = info.get("duration", 0)
                is_video = info.get("is_video", True) or bool(info.get("duration"))
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for f in os.listdir(tempfile.gettempdir()):
                        if f.startswith(os.path.basename(base)):
                            filename = os.path.join(tempfile.gettempdir(), f)
                            break
                return filename, title, duration, is_video

        filename, title, duration, is_video = await loop.run_in_executor(None, download)

        file_size = os.path.getsize(filename)

        if file_size > 50 * 1024 * 1024:
            await processing_msg.edit_text(
                "❌ El archivo pesa más de 50 MB.\n"
                "Telegram no permite enviar archivos tan grandes a través de bots normales."
            )
            os.remove(filename)
            return

        await processing_msg.edit_text("📤 Subiendo a Telegram...")

        if is_video:
            with open(filename, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"📥 Descargado por @{context.bot.username}\n🔗 {url}",
                    duration=duration if duration else None,
                    supports_streaming=True,
                )
        else:
            with open(filename, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"📥 Descargado por @{context.bot.username}\n🔗 {url}",
                )

        os.remove(filename)

    except yt_dlp.utils.DownloadError as e:
        await processing_msg.edit_text(f"❌ Error de descarga:\n`{str(e)}`", parse_mode="Markdown")
    except Exception as e:
        msg = str(e)[:200]
        await processing_msg.edit_text(f"❌ Error inesperado:\n`{msg}`", parse_mode="Markdown")
    else:
        await processing_msg.delete()

application.add_handler(CommandHandler("start", start))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

_bot_loop = None
_bot_ready = False
_bot_lock = threading.Lock()

async def _init_bot():
    global _bot_ready
    try:
        await application.initialize()
        await application.start()
        if RENDER_EXTERNAL_URL:
            webhook_url = f"https://{RENDER_EXTERNAL_URL}/webhook"
            await application.bot.set_webhook(url=webhook_url)
            logging.info(f"Webhook configurado en {webhook_url}")
        _bot_ready = True
    except Exception as e:
        logging.error(f"Error inicializando bot: {e}")

def ensure_bot():
    global _bot_loop, _bot_ready
    if _bot_ready:
        return
    with _bot_lock:
        if _bot_ready:
            return
        _bot_loop = asyncio.new_event_loop()
        t = threading.Thread(target=_bot_loop.run_forever, daemon=True)
        t.start()
        fut = asyncio.run_coroutine_threadsafe(_init_bot(), _bot_loop)
        fut.result(timeout=30)

@app.route("/webhook", methods=["POST"])
def webhook():
    ensure_bot()
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), _bot_loop)
    return "OK", 200

@app.route("/")
@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
