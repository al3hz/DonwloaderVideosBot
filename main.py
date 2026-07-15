import os
import re
import json
import time
import asyncio
import threading
import tempfile
import logging
import subprocess
import concurrent.futures
from contextlib import contextmanager
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
import requests

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
BLACKLIST_FILE = os.path.join(tempfile.gettempdir(), "blacklist.json")

# --- Configuración robusta de yt-dlp ---
YTDLP_COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "no_check_certificates": True,
    "retries": 10,
    "fragment_retries": 10,
    "file_access_retries": 3,
    "extractor_retries": 3,
    "socket_timeout": 30,
    "throttled_rate": "100K",
    "merge_output_format": "mp4",
    "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    },
}

def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            return set(json.load(f))
    return set()

def save_blacklist(domains):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(list(domains), f)

app = Flask(__name__)
_download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")

# --- Context manager para limpieza de archivos ---
@contextmanager
def temp_file_cleanup():
    paths = []
    try:
        yield paths
    finally:
        for p in paths:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception as e:
                logging.warning(f"No se pudo eliminar {p}: {e}")

# --- Progress tracker con throttling anti-rate-limit ---
class ProgressTracker:
    def __init__(self, loop, message):
        self.loop = loop
        self.message = message
        self.last_pct = -1
        self.last_update = 0
        self._closed = False
        self._lock = threading.Lock()

    def hook(self, d):
        if d["status"] != "downloading":
            return
        try:
            raw = d.get("_percent_str", "").strip().replace("%", "")
            pct = float(raw)
        except Exception:
            return

        with self._lock:
            now = time.time()
            if pct - self.last_pct < 10 and now - self.last_update < 3 and pct != 100:
                return
            if self._closed or not self.loop or self.loop.is_closed():
                return
            self.last_pct = pct
            self.last_update = now

        try:
            asyncio.run_coroutine_threadsafe(
                self.message.edit_text(f"⏳ Descargando... {pct:.0f}%"),
                self.loop,
            )
        except Exception:
            pass

    def close(self):
        self._closed = True

def get_ydl_opts(progress_hook=None, extra_opts=None):
    opts = dict(YTDLP_COMMON_OPTS)
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    if extra_opts:
        opts.update(extra_opts)
    return opts

# --- Descarga con fallback de formatos (ELIMINA filesize<50M) ---
def download_with_fallback(url, progress_hook=None):
    attempts = [
        # Intento 1: H.264 1080p + AAC (máxima compatibilidad)
        {
            "format": "bestvideo[height<=1080][vcodec^=avc]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "merge_output_format": "mp4",
        },
        # Intento 2: Cualquier video 1080p
        {
            "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
            "merge_output_format": "mp4",
        },
        # Intento 3: Cualquier cosa que funcione
        {
            "format": "best/bestvideo+bestaudio",
            "merge_output_format": "mp4",
        },
        # Intento 4: Forzar extractor genérico (Reddit con URLs directas)
        {
            "format": "best",
            "force_generic_extractor": True,
        },
    ]

    last_error = None
    for i, attempt_opts in enumerate(attempts, 1):
        try:
            opts = get_ydl_opts(progress_hook, attempt_opts)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
                title = info.get("title", "Video")
                duration = info.get("duration", 0)
                is_video = bool(info.get("duration")) or info.get("is_video", True)

                # yt-dlp a veces cambia la extensión tras merge
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for f in os.listdir(tempfile.gettempdir()):
                        if f.startswith(os.path.basename(base)):
                            candidate = os.path.join(tempfile.gettempdir(), f)
                            if os.path.getsize(candidate) > 1024:
                                filename = candidate
                                break

                if not os.path.exists(filename) or os.path.getsize(filename) < 1024:
                    raise yt_dlp.utils.DownloadError("Archivo descargado vacío o inexistente")

                return filename, title, duration, is_video, None

        except Exception as e:
            last_error = e
            logging.warning(f"Intento {i} falló para {url}: {e}")
            continue

    raise last_error

# --- TikTok API fallback ---
def _tiktok_api_fallback(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        resp = requests.post(
            "https://tikwm.com/api/",
            data={"url": url.split("?")[0]},
            headers=headers,
            timeout=30
        )
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
    except Exception as e:
        logging.warning(f"TikTok API fallback falló: {e}")
        return None

# --- Handlers del bot ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 Envíame un enlace de:\n"
        "• TikTok (sin marca de agua)\n"
        "• YouTube / YouTube Shorts\n"
        "• Twitter / X\n"
        "• Instagram (Reels / Fotos)\n"
        "• Facebook\n"
        "• Reddit\n"
        "• Y muchas más...\n\n"
        "⚠️ Límite: 50 MB por archivo (límite de Telegram para bots).\n\n"
        "📋 Comandos:\n"
        "  /blacklist — Ver /blacklist add <dominio> /blacklist remove <dominio>"
    )
    await update.message.reply_text(text)

async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bl = load_blacklist()
    args = context.args
    if not args:
        if bl:
            text = "🚫 Dominios en lista negra:\n" + "\n".join(f"• `{d}`" for d in sorted(bl))
        else:
            text = "✅ No hay dominios en la lista negra."
        await update.message.reply_text(text, parse_mode="Markdown")
        return
    action = args[0].lower()
    if action == "add" and len(args) >= 2:
        domain = args[1].lower().strip()
        bl.add(domain)
        save_blacklist(bl)
        await update.message.reply_text(f"✅ `{domain}` añadido a la lista negra.", parse_mode="Markdown")
    elif action == "remove" and len(args) >= 2:
        domain = args[1].lower().strip()
        bl.discard(domain)
        save_blacklist(bl)
        await update.message.reply_text(f"✅ `{domain}` eliminado de la lista negra.", parse_mode="Markdown")
    else:
        await update.message.reply_text(
            "Uso: /blacklist — ver lista\n/blacklist add <dominio>\n/blacklist remove <dominio>"
        )

async def tiktok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = f"tiktok:{query.message.message_id}"
    stored = context.user_data.pop(key, None)
    if not stored:
        await query.edit_message_text("❌ Esta solicitud ya expiró.")
        return

    url = stored["url"]
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

    with temp_file_cleanup() as cleanup:
        cleanup.append(filename)
        try:
            r = requests.get(img_url, timeout=60)
            r.raise_for_status()
            with open(filename, "wb") as f:
                f.write(r.content)

            await query.edit_message_text("📤 Subiendo a Telegram...")
            with open(filename, "rb") as f:
                await query.message.reply_photo(
                    photo=f,
                    caption=f"📥 Descargado por @{context.bot.username}\n🔗 {url}",
                )
            try:
                await query.delete_message()
            except Exception:
                pass
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {str(e)[:200]}")

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    blacklist = load_blacklist()
    if any(domain in url.lower() for domain in blacklist):
        await update.message.reply_text("🚫 Este dominio está en la lista negra.")
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")
    loop = asyncio.get_running_loop()

    is_tiktok = "tiktok.com" in url

    # --- TikTok slideshow ---
    if is_tiktok:
        clean_url = url.split("?")[0]
        if "/photo/" in clean_url:
            clean_url = clean_url.replace("/photo/", "/video/")

        slideshow_data = None
        try:
            def try_ydl():
                with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
                    return ydl.extract_info(clean_url, download=False)
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
            await processing_msg.edit_text("⏳ Descargando imágenes...")

            def dl_slideshow():
                paths = []
                targets = api_images if api_images else [sf.get("url") for sf in slideshow_formats]
                for i, img_url in enumerate(targets):
                    if not img_url:
                        continue
                    path = os.path.join(tempfile.gettempdir(), f"tiktok_slide_{int(time.time())}_{i}.jpg")
                    try:
                        r = requests.get(img_url, timeout=60)
                        r.raise_for_status()
                        with open(path, "wb") as f:
                            f.write(r.content)
                        paths.append(path)
                    except Exception as e:
                        logging.warning(f"Fallo descarga imagen {i}: {e}")
                return paths

            img_paths = await loop.run_in_executor(_download_executor, dl_slideshow)
            if not img_paths:
                await processing_msg.edit_text("❌ No se pudieron descargar las imágenes.")
                return

            with temp_file_cleanup() as cleanup:
                cleanup.extend(img_paths)
                await processing_msg.edit_text("📤 Subiendo a Telegram...")
                caption_text = f"📥 Descargado por @{context.bot.username}\n🔗 {url}"

                # Abrimos los archivos ordenadamente para armar el media group
                opened_files = []
                try:
                    for i, path in enumerate(img_paths[:10]):  # Límite máximo de 10 elementos por grupo
                        f = open(path, "rb")
                        opened_files.append(f)
                        if i == 0:
                            media_group = [InputMediaPhoto(f, caption=caption_text)]
                        else:
                            media_group.append(InputMediaPhoto(f))
                    
                    await update.message.reply_media_group(media_group)
                finally:
                    for f in opened_files:
                        f.close()

            try:
                await processing_msg.delete()
            except Exception:
                pass
            return

    # --- Descarga normal con fallback ---
    tracker = ProgressTracker(loop, processing_msg)

    with temp_file_cleanup() as cleanup:
        try:
            result = await loop.run_in_executor(
                _download_executor,
                download_with_fallback,
                url,
                tracker.hook
            )
            filename, title, duration, is_video, _ = result
            cleanup.append(filename)
            tracker.close()

            file_size = os.path.getsize(filename)
            if file_size > 50 * 1024 * 1024:
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

            try:
                await processing_msg.delete()
            except Exception:
                pass

        except yt_dlp.utils.DownloadError as e:
            err_msg = str(e)
            if "Sign in to confirm" in err_msg or "confirm your age" in err_msg:
                friendly = "❌ YouTube requiere verificación de edad o login. Prueba con otro video."
            elif "Private video" in err_msg:
                friendly = "❌ Este video es privado."
            elif "This video is not available" in err_msg:
                friendly = "❌ Este video no está disponible (puede estar restringido por región)."
            elif "HTTP Error 429" in err_msg or "too many requests" in err_msg.lower():
                friendly = "❌ Demasiadas peticiones. Espera unos minutos e intenta de nuevo."
            elif "Unable to extract" in err_msg:
                friendly = "❌ No se pudo extraer el video. El sitio puede haber cambiado su estructura."
            elif "Requested format is not available" in err_msg:
                friendly = "❌ No se encontró un formato compatible. Prueba con otro enlace."
            else:
                friendly = f"❌ Error de descarga:\n`{err_msg[:300]}`"

            try:
                await processing_msg.edit_text(friendly, parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(friendly, parse_mode="Markdown")

        except Exception as e:
            logging.exception("Error inesperado en download_video")
            msg = str(e)[:300]
            try:
                await processing_msg.edit_text(f"❌ Error inesperado:\n`{msg}`", parse_mode="Markdown")
            except Exception:
                await update.message.reply_text(f"❌ Error inesperado:\n`{msg}`", parse_mode="Markdown")

# --- Setup del bot ---
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("blacklist", blacklist_cmd))
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
        
