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
COOKIES_PATH = os.path.join(tempfile.gettempdir(), "instagram_cookies.txt")

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
        "⚠️ Límite: 50 MB por video (límite de Telegram para bots).\n\n"
        "📘 Para Instagram: usa /cookies para configurar autenticación."
    )
    await update.message.reply_text(text)

async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📘 Para descargar de Instagram necesito cookies.\n\n"
        "1. Instala esta extensión: https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid\n"
        "2. Inicia sesión en Instagram en tu navegador\n"
        "3. Haz clic en la extensión y exporta las cookies\n"
        "4. Envíame el archivo .txt aquí mismo\n\n"
        "Las cookies se guardan solo para tus descargas."
    )

async def handle_cookies_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        return

    file = await update.message.document.get_file()
    await file.download_to_drive(COOKIES_PATH)
    await update.message.reply_text("✅ Cookies de Instagram guardadas correctamente. Ya puedes descargar videos de IG.")

def get_ydl_opts():
    opts = {
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    return opts

def download_instagram(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    html = resp.text

    json_match = re.search(r'<script type="application/json" data-sjs>(.*?)</script>', html)
    if not json_match:
        json_match = re.search(r'window\.__INITIAL_STATE__\s*=\s*({.*?});', html)

    video_url = None
    title = "Instagram Video"

    og_video = re.search(r'<meta\s+property="og:video"\s+content="([^"]+)"', html)
    if og_video:
        video_url = og_video.group(1)

    if not video_url:
        og_video = re.search(r'<meta\s+property="og:video:secure_url"\s+content="([^"]+)"', html)
        if og_video:
            video_url = og_video.group(1)

    if not video_url:
        og_video = re.search(r'<video[^>]+src="([^"]+)"', html)
        if og_video:
            video_url = og_video.group(1)

    if not video_url and json_match:
        try:
            data = json.loads(json_match.group(1))
            media = None
            items = None

            if "entry_data" in data and "PostPage" in data["entry_data"]:
                items = data["entry_data"]["PostPage"][0]["graphql"]["shortcode_media"]
            elif "graphql" in data and "shortcode_media" in data.get("graphql", {}):
                items = data["graphql"]["shortcode_media"]

            if items:
                if items.get("is_video") and items.get("video_url"):
                    video_url = items["video_url"]
                    title = items.get("edge_media_to_caption", {}).get("edges", [{}])[0].get("node", {}).get("text", "Instagram Video") or "Instagram Video"
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            logging.warning(f"Error parsing Instagram JSON: {e}")

    if not video_url:
        raise Exception("No se pudo encontrar el video en la página de Instagram.")

    vid_resp = requests.get(video_url, headers=headers, timeout=60)
    vid_resp.raise_for_status()

    ext = "mp4"
    cd = vid_resp.headers.get("Content-Disposition", "")
    if "." in cd:
        ext = cd.split(".")[-1].strip("\"'")
    elif "?" in video_url:
        clean = video_url.split("?")[0]
        if "." in clean:
            ext = clean.split(".")[-1]

    filename = os.path.join(tempfile.gettempdir(), f"instagram_{int(time.time())}.{ext}")
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

        is_instagram = "instagram.com" in url

        def download():
            if is_instagram:
                return download_instagram(url)
            with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
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
application.add_handler(CommandHandler("cookies", cookies_command))
application.add_handler(MessageHandler(filters.Document.FileExtension("txt"), handle_cookies_file))
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
