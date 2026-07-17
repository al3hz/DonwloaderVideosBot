import os
import re
import json
import time
import asyncio
import threading
import tempfile
import logging
import concurrent.futures
from flask import Flask, request
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
import requests 

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

ALLOWED_DOMAINS = ["tiktok.com", "instagram.com", "twitter.com", "x.com"]

COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.path.join(tempfile.gettempdir(), "cookies.txt")

app = Flask(__name__)

_download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

async def start(update: Update, _context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 Envíame un enlace de:\n"
        "• TikTok (sin marca de agua)\n"
        "• Instagram (Reels)\n"
        "• Twitter / X\n\n"
        "⚠️ Límite: 50 MB por archivo (límite de Telegram para bots)."
    )
    await update.message.reply_text(text)

class ProgressTracker:
    def __init__(self, loop, message):
        self.loop = loop
        self.message = message
        self.last_pct = -1
        self._closed = False
        self._lock = threading.Lock()

    def hook(self, d):
        if d["status"] == "downloading":
            try:
                raw = d.get("_percent_str", "").strip().replace("%", "")
                pct = float(raw)
                if pct - self.last_pct >= 5 or (pct == 100 and self.last_pct != 100):
                    self.last_pct = pct
                    with self._lock:
                        closed = self._closed
                    if not closed and self.loop and not self.loop.is_closed():
                        asyncio.run_coroutine_threadsafe(
                            self.message.edit_text(f"⏳ Descargando... {pct:.0f}%"),
                            self.loop,
                        )
            except Exception:
                pass

    def close(self):
        with self._lock:
            self._closed = True

def get_ydl_opts(progress_hook=None):
    opts = {
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "socket_timeout": 120,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "quiet": True,
        "no_warnings": True,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts

def _tiktok_api_fallback(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    resp = requests.post("https://tikwm.com/api/", data={"url": url.split("?")[0]}, headers=headers, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        return None
    result = data.get("data", {})
    images = result.get("images", [])
    music_url = result.get("music_info", {}).get("play", "")
    if not images:
        return None
    slideshow_formats = [{"url": img, "ext": "jpg"} for img in images]
    audio_formats = []
    if music_url:
        audio_formats = [{"url": music_url, "ext": "mp3"}]
    return (slideshow_formats, audio_formats, images)



async def tiktok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = f"tiktok:{query.message.chat_id}:{query.message.message_id}"
    stored = context.user_data.pop(key, None)
    if not stored:
        await query.edit_message_text("❌ Esta solicitud ya expiró.")
        return
    url = stored["url"]
    loop = asyncio.get_running_loop()

    proc_id = stored.get("processing_msg_id")
    if proc_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=proc_id)
        except Exception:
            pass

    await query.edit_message_text("⏳ Descargando imagen...")
    sf = stored["slideshow_format"]
    img_url = sf.get("url")
    if not img_url:
        await query.edit_message_text("❌ No se pudo obtener la URL de la imagen.")
        return
    ext = sf.get("ext", "jpg")
    filename = os.path.join(tempfile.gettempdir(), f"tiktok_img_{int(time.time())}.{ext}")
    r = requests.get(img_url, timeout=60)
    r.raise_for_status()
    with open(filename, "wb") as f:
        f.write(r.content)
    await query.edit_message_text("📤 Subiendo a Telegram...")
    with open(filename, "rb") as f:
        await query.message.reply_photo(
            photo=f,
            caption=f"📥 Descargado por @{stored['context'].bot.username}",
        )
    os.remove(filename)
    try:
        await query.delete_message()
    except Exception:
        pass

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
        await update.message.reply_text(
            "❌ Solo acepto enlaces de TikTok, Instagram y Twitter/X."
        )
        return

    if "instagram.com/p/" in url:
        await update.message.reply_text(
            "❌ Solo acepto Reels de Instagram, no fotos estáticas."
        )
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        loop = asyncio.get_running_loop()
        is_tiktok = "tiktok.com" in url

        # --- TikTok slideshow detection ---
        if is_tiktok:
            clean_url = url.split("?")[0]
            if "/photo/" in clean_url:
                clean_url = clean_url.replace("/photo/", "/video/")

            def try_ydl():
                with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
                    return ydl.extract_info(clean_url, download=False)

            slideshow_data = None
            try:
                info = await loop.run_in_executor(_download_executor, try_ydl)
                formats = info.get("formats", [])
                slideshow_formats = [f for f in formats if f.get("format_id", "").startswith("slideshow-")]
                if slideshow_formats:
                    audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec", "none") != "none"]
                    slideshow_data = (slideshow_formats, audio_formats, None)
            except Exception:
                pass

            if not slideshow_data:
                try:
                    api_data = await loop.run_in_executor(_download_executor, _tiktok_api_fallback, url)
                    if api_data:
                        slideshow_data = api_data
                except Exception:
                    pass

            if slideshow_data:
                slideshow_formats, audio_formats, api_images = slideshow_data

                if len(slideshow_formats) == 1 and audio_formats:
                    pass  # falls through to multi-image logic below (downloads as single image)

                await processing_msg.edit_text("⏳ Descargando imágenes...")

                def dl_slideshow():
                    paths = []
                    targets = api_images if api_images else [sf.get("url") for sf in slideshow_formats]
                    for i, img_url in enumerate(targets):
                        if not img_url:
                            continue
                        ext = "jpg"
                        path = os.path.join(
                            tempfile.gettempdir(),
                            f"tiktok_slide_{int(time.time())}_{i}.{ext}",
                        )
                        r = requests.get(img_url, timeout=60)
                        r.raise_for_status()
                        with open(path, "wb") as f:
                            f.write(r.content)
                        paths.append(path)
                    return paths

                img_paths = await loop.run_in_executor(_download_executor, dl_slideshow)
                if not img_paths:
                    await processing_msg.edit_text("❌ No se pudieron descargar las imágenes.")
                    return

                await processing_msg.edit_text("📤 Subiendo a Telegram...")
                caption_text = f"📥 Descargado por @{context.bot.username}"
                for batch_start in range(0, len(img_paths), 10):
                    batch = img_paths[batch_start:batch_start + 10]
                    media_group = []
                    files = []
                    for i, path in enumerate(batch):
                        f = open(path, "rb")
                        files.append(f)
                        if batch_start == 0 and i == 0:
                            media_group.append(InputMediaPhoto(f, caption=caption_text))
                        else:
                            media_group.append(InputMediaPhoto(f))
                    await update.message.reply_media_group(media_group)
                    for f in files:
                        f.close()
                for p in img_paths:
                    os.remove(p)
                try:
                    await processing_msg.delete()
                except Exception:
                    pass
                return

        # --- Normal download ---
        tracker = ProgressTracker(loop, processing_msg)

        def download():
            opts = get_ydl_opts(tracker.hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                requested = info.get("requested_downloads")
                if requested:
                    filename = requested[0].get("filepath", ydl.prepare_filename(info))
                else:
                    filename = ydl.prepare_filename(info)
                title = info.get("title", "Video")
                duration = info.get("duration", 0)
                is_video = info.get("is_video", True) or bool(info.get("duration"))
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in [".mp4", ".webm", ".mkv", ".jpg", ".png", ".webp"]:
                        candidate = base + ext
                        if os.path.exists(candidate):
                            filename = candidate
                            break
                return filename, title, duration, is_video, False

        result = await loop.run_in_executor(_download_executor, download)
        filename, title, duration, is_video, is_photo = result
        file_size = os.path.getsize(filename)

        if file_size > 50 * 1024 * 1024:
            os.remove(filename)
            await processing_msg.edit_text(
                "❌ El archivo pesa más de 50 MB.\n"
                "Telegram no permite enviar archivos tan grandes a través de bots normales."
            )
            return

        await processing_msg.edit_text("📤 Subiendo a Telegram...")

        if is_video:
            with open(filename, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"📥 Descargado por @{context.bot.username}",
                    duration=duration if duration else None,
                    supports_streaming=True,
                    read_timeout=120,
                )
        else:
            with open(filename, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"📥 Descargado por @{context.bot.username}",
                    read_timeout=120,
                )

        os.remove(filename)

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        friendly = {
            "No video could be found in this tweet": (
                "❌ No se pudo encontrar un video en ese tweet.\n"
                "Asegúrate de que el tweet contiene un video nativo de X (no un enlace externo)."
            ),
            "Requested format is not available": (
                "❌ No hay un formato de video disponible para este enlace."
            ),
            "This video is only available for registered users": (
                "❌ Este video requiere inicio de sesión en la plataforma."
            ),
        }
        for key, msg in friendly.items():
            if key in err_msg:
                display_msg = msg
                break
        else:
            display_msg = f"❌ Error de descarga:\n`{err_msg[:200]}`"
        try:
            await processing_msg.edit_text(display_msg, parse_mode="Markdown")
        except Exception:
            try:
                await update.message.reply_text(display_msg, parse_mode="Markdown")
            except Exception:
                pass
    except Exception as e:
        msg = str(e)[:200]
        display_msg = f"❌ Error inesperado:\n`{msg}`"
        try:
            await processing_msg.edit_text(display_msg, parse_mode="Markdown")
        except Exception:
            try:
                await update.message.reply_text(display_msg, parse_mode="Markdown")
            except Exception:
                pass
    else:
        await processing_msg.delete()

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(tiktok_callback, pattern="^tiktok_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

_bot_loop = None
_bot_ready = False
_bot_lock = threading.Lock()

async def _init_bot():
    await application.initialize()
    await application.start()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"https://{RENDER_EXTERNAL_URL}/webhook"
        await application.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook configurado en {webhook_url}")

def ensure_bot():
    global _bot_loop, _bot_ready
    if _bot_ready:
        return True
    with _bot_lock:
        if _bot_ready:
            return True
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_init_bot())
            _bot_loop = loop
            t = threading.Thread(target=_bot_loop.run_forever, daemon=True)
            t.start()
            _bot_ready = True
            logging.info("Bot inicializado correctamente")
            return True
        except Exception as e:
            logging.error(f"Error inicializando bot: {e}")
            return False

@app.route("/webhook", methods=["POST"])
def webhook():
    if not ensure_bot():
        return "Bot not ready", 503
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), _bot_loop)
    return "OK", 200

@app.route("/")
@app.route("/health")
def health():
    if not _bot_ready:
        return "Bot not ready", 503
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
