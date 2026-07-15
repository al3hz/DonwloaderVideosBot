import os
import re
import json
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
        "• Instagram Reels\n"
        "• Facebook\n"
        "• Y muchas más...\n\n"
        "⚠️ Límite: 50 MB por video (límite de Telegram para bots)."
    )
    await update.message.reply_text(text)

DOWNLOAD_DIR = tempfile.gettempdir()

def get_ydl_opts():
    return {
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }

def download_pornhub(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.pornhub.com/",
    }

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    html = resp.text

    video_url = None
    title = "PornHub Video"

    # Try to find video URL in various patterns
    patterns = [
        r'videoUrl\s*=\s*["\']([^"\']+)["\']',
        r'"video_url"\s*:\s*"([^"]+)"',
        r'qualityItems\s*=\s*(\[.*?\]);',
        r'data-video-url\s*=\s*"([^"]+)"',
        r'mediaDefinitions\s*:\s*(\[.*?\])',
        r'<video[^>]+>.*?<source[^>]+src="([^"]+)"',
        r'<meta\s+property="og:video"\s+content="([^"]+)"',
        r'<meta\s+property="og:video:secure_url"\s+content="([^"]+)"',
        r'var\s+videoUrl\s*=\s*"([^"]+)"',
        r'let\s+videoUrl\s*=\s*"([^"]+)"',
        r'const\s+videoUrl\s*=\s*"([^"]+)"',
        r'"defaultQuality"\s*:\s*"[^"]*".*?"url"\s*:\s*"([^"]+)"',
        r'videoUrl["\']?\s*[:=]\s*["\']([^"\']+)["\']',
    ]

    for p in patterns:
        m = re.search(p, html, re.IGNORECASE | re.DOTALL)
        if m:
            found = m.group(1)
            if found.startswith("http") or found.startswith("//"):
                video_url = found
                break

    # Try JSON extraction from script tags
    if not video_url:
        script_patterns = [
            r'window\.__INITIAL_STATE__\s*=\s*({.*?});',
            r'window\.__NUXT__\s*=\s*({.*?});',
            r'<script[^>]*>\s*window\.__data\s*=\s*({.*?});\s*</script>',
            r'<script[^>]*>\s*window\.pageData\s*=\s*({.*?});\s*</script>',
        ]
        for sp in script_patterns:
            m = re.search(sp, html, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(1))
                    raw = json.dumps(data)
                    vu = re.search(r'"videoUrl"\s*:\s*"([^"]+)"', raw)
                    if vu:
                        video_url = vu.group(1).replace("\\/", "/")
                    if not video_url:
                        vu = re.search(r'"url"\s*:\s*"([^"]+\.mp4[^"]*)"', raw)
                        if vu:
                            video_url = vu.group(1).replace("\\/", "/")
                except json.JSONDecodeError:
                    pass

    if not video_url:
        raise Exception("No se pudo encontrar el video en la página de PornHub.")

    if video_url.startswith("//"):
        video_url = "https:" + video_url

    vid_resp = requests.get(video_url, headers=headers, timeout=120)
    vid_resp.raise_for_status()

    filename = os.path.join(DOWNLOAD_DIR, f"pornhub_{int(time.time())}.mp4")
    with open(filename, "wb") as f:
        f.write(vid_resp.content)

    return filename, title, 0

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        loop = asyncio.get_running_loop()
        is_pornhub = "pornhub.com" in url

        def download():
            if is_pornhub:
                return download_pornhub(url)
            with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                title = info.get("title", "Video")
                duration = info.get("duration", 0)
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for f in os.listdir(DOWNLOAD_DIR):
                        if f.startswith(os.path.basename(base)):
                            filename = os.path.join(DOWNLOAD_DIR, f)
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
                caption=f"📥 Descargado por @{context.bot.username}\n🔗 {url}",
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
