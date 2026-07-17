import os
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

# Configuración desde variables de entorno
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")

# Dominios permitidos para descarga
ALLOWED_DOMAINS = ["tiktok.com", "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.com", "youtube.com", "youtu.be"]

# Archivo de cookies opcional para autenticación en plataformas
COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.path.join(tempfile.gettempdir(), "cookies.txt")

app = Flask(__name__)

# Executor para descargas en segundo plano (máximo 2 simultáneas)
_download_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")

application = (
    Application.builder()
    .token(TOKEN)
    .build()
)

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Mensaje de bienvenida con las plataformas soportadas."""
    user = update.effective_user
    logging.info(f"Comando /start de {user.id} (@{user.username})")
    text = (
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 Envíame un enlace de:\n"
        "• TikTok (sin marca de agua)\n"
        "• Instagram (Reels)\n"
        "• Facebook (videos / Reels)\n"
        "• Twitter / X (Videos / GIF)\n"
        "• YouTube (videos públicos / Shorts)\n\n"
        "⚠️ Límite: 50 MB por archivo.\n"
        "⚠️ YouTube: solo videos públicos. No funcionan videos privados, "
        "con restricción de edad ni livestreams."
    )
    await update.message.reply_text(text)

class ProgressTracker:
    """Rastrea el progreso de descarga de yt-dlp (callback interno)."""

    def __init__(self):
        logging.debug("ProgressTracker inicializado")

    def hook(self, d):
        """Callback de yt-dlp (no se muestra progreso al usuario, solo ⏳)."""
        pass

def get_ydl_opts(progress_hook=None):
    """Retorna las opciones de configuración para yt-dlp con los parámetros del proyecto."""
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
        logging.info(f"Usando cookies desde {COOKIES_FILE}")
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts

def _tiktok_api_fallback(url):
    """Fallback para TikTok slideshows usando la API de tikwm.com cuando yt-dlp falla."""
    logging.info(f"Usando fallback tikwm.com para {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    resp = requests.post("https://tikwm.com/api/", data={"url": url.split("?")[0]}, headers=headers, timeout=30)
    data = resp.json()
    if data.get("code") != 0:
        logging.warning(f"tikwm.com respondió con código {data.get('code')}")
        return None
    result = data.get("data", {})
    images = result.get("images", [])
    music_url = result.get("music_info", {}).get("play", "")
    if not images:
        logging.warning("tikwm.com no devolvió imágenes")
        return None
    logging.info(f"tikwm.com devolvió {len(images)} imágenes")
    slideshow_formats = [{"url": img, "ext": "jpg"} for img in images]
    audio_formats = []
    if music_url:
        audio_formats = [{"url": music_url, "ext": "mp3"}]
    return (slideshow_formats, audio_formats, images)



async def tiktok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los callbacks de botones inline para descargar imágenes individuales de TikTok slideshows."""
    query = update.callback_query
    await query.answer()
    key = f"tiktok:{query.message.chat_id}:{query.message.message_id}"
    stored = context.user_data.pop(key, None)
    if not stored:
        logging.warning(f"Callback expirado para {key}")
        await query.edit_message_text("❌ Esta solicitud ya expiró.")
        return

    proc_id = stored.get("processing_msg_id")
    if proc_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=proc_id)
        except Exception:
            pass

    logging.info(f"Descargando slide de TikTok para {query.message.chat_id}")
    await query.edit_message_text("⏳ Descargando imagen...")
    sf = stored["slideshow_format"]
    img_url = sf.get("url")
    if not img_url:
        logging.error("No se encontró URL de imagen en slideshow_format")
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
    """Procesa un enlace enviado por el usuario: detecta plataforma, descarga y envía el video."""
    url = update.message.text.strip()
    user = update.effective_user
    logging.info(f"URL recibida de {user.id}: {url}")

    # Validación básica de URL
    if not url.startswith(("http://", "https://")):
        logging.warning(f"URL inválida de {user.id}: {url}")
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    # Filtro por dominios permitidos
    if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
        logging.warning(f"Dominio no permitido de {user.id}: {url}")
        await update.message.reply_text(
            "❌ Solo acepto enlaces de TikTok, Instagram, Facebook, Twitter/X y YouTube."
        )
        return

    # Instagram: solo Reels, rechazar fotos estáticas /p/
    if "instagram.com/p/" in url:
        logging.info(f"URL de foto IG rechazada: {url}")
        await update.message.reply_text(
            "❌ Solo acepto Reels de Instagram, no fotos estáticas."
        )
        return

    processing_msg = await update.message.reply_text("⏳")

    try:
        loop = asyncio.get_running_loop()
        is_tiktok = "tiktok.com" in url

        # --- Detección y manejo de TikTok slideshows (/photo/) ---
        if is_tiktok:
            logging.info(f"URL de TikTok detectada: {url}")
            clean_url = url.split("?")[0]
            if "/photo/" in clean_url:
                clean_url = clean_url.replace("/photo/", "/video/")
                logging.info(f"URL convertida para slideshow: {clean_url}")

            # Intento 1: detectar slideshow via yt-dlp
            def try_ydl():
                logging.debug("try_ydl: extrayendo info via yt-dlp")
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
                    logging.info(f"Slideshow detectado via yt-dlp: {len(slideshow_formats)} slides")
            except Exception as e:
                logging.warning(f"yt-dlp falló detectando slideshow: {e}")

            # Intento 2: fallback via API de tikwm.com si yt-dlp no detectó slideshow
            if not slideshow_data:
                try:
                    api_data = await loop.run_in_executor(_download_executor, _tiktok_api_fallback, url)
                    if api_data:
                        slideshow_data = api_data
                except Exception as e:
                    logging.warning(f"Fallback tikwm también falló: {e}")

            # Si se detectó slideshow, descargar imágenes y enviar como álbum
            if slideshow_data:
                slideshow_formats, audio_formats, api_images = slideshow_data
                logging.info(f"Procesando slideshow con {len(slideshow_formats)} imágenes")

                if len(slideshow_formats) == 1 and audio_formats:
                    pass  # Un solo slide con audio se maneja igual que múltiples

                # Descarga todas las imágenes del slideshow
                def dl_slideshow():
                    logging.debug("dl_slideshow: descargando imagenes")
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

        # --- Descarga normal con yt-dlp (TikTok videos, Instagram Reels, Twitter/X) ---
        logging.info(f"Iniciando descarga yt-dlp para: {url}")

        # Función bloqueante que corre en el executor para no bloquear el event loop
        def download():
            logging.debug("download: iniciando yt-dlp")
            opts = get_ydl_opts()
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                # Obtener la ruta real del archivo descargado
                requested = info.get("requested_downloads")
                if requested:
                    filename = requested[0].get("filepath", ydl.prepare_filename(info))
                else:
                    filename = ydl.prepare_filename(info)
                duration = info.get("duration", 0)
                is_video = info.get("is_video", True) or bool(info.get("duration"))
                # Fallback por si la extensión real difiere de la esperada
                if not os.path.exists(filename):
                    base = os.path.splitext(filename)[0]
                    for ext in [".mp4", ".webm", ".mkv", ".jpg", ".png", ".webp"]:
                        candidate = base + ext
                        if os.path.exists(candidate):
                            filename = candidate
                            break
                return filename, duration, is_video

        result = await loop.run_in_executor(_download_executor, download)
        filename, duration, is_video = result
        file_size = os.path.getsize(filename)
        logging.info(f"Descarga completada: {filename} ({file_size} bytes)")

        # Verificar límite de 50 MB de Telegram
        if file_size > 50 * 1024 * 1024:
            logging.warning(f"Archivo excede 50MB: {filename}")
            os.remove(filename)
            await processing_msg.edit_text(
                "❌ El archivo pesa más de 50 MB.\n"
                "Telegram no permite enviar archivos tan grandes a través de bots normales."
            )
            return

        # Detectar si el archivo tiene audio (si no, se envía como GIF animado)
        import subprocess
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "a", "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", filename],
                capture_output=True, text=True, timeout=10
            )
            has_audio_stream = bool(probe.stdout.strip())
        except Exception:
            has_audio_stream = True

        if is_video and not has_audio_stream:
            with open(filename, "rb") as f:
                await update.message.reply_animation(
                    animation=f,
                    caption=f"📥 Descargado por @{context.bot.username}",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            logging.info(f"GIF enviado: {filename}")
        elif is_video:
            with open(filename, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"📥 Descargado por @{context.bot.username}",
                    duration=duration if duration else None,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            logging.info(f"Video enviado: {filename}")
        else:
            with open(filename, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"📥 Descargado por @{context.bot.username}",
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=30,
                )
            logging.info(f"Foto enviada: {filename}")

        os.remove(filename)

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)
        logging.error(f"DownloadError para {url}: {err_msg}")
        # Mapeo de errores conocidos a mensajes amigables en español
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
        logging.error(f"Error inesperado para {url}: {msg}")
        display_msg = f"❌ Error inesperado:\n`{msg}`"
        try:
            await processing_msg.edit_text(display_msg, parse_mode="Markdown")
        except Exception:
            try:
                await update.message.reply_text(display_msg, parse_mode="Markdown")
            except Exception:
                pass
    else:
        logging.info(f"Descarga exitosa para: {url}")
        # Si todo salió bien, eliminar el mensaje de progreso
        await processing_msg.delete()

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(tiktok_callback, pattern="^tiktok_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_video))

# Variables globales para el loop asyncio del bot
_bot_loop = None
_bot_ready = False
_bot_lock = threading.Lock()

async def _init_bot():
    """Inicializa la aplicación de python-telegram-bot y configura el webhook si aplica."""
    logging.info("Inicializando bot...")
    await application.initialize()
    await application.start()
    if RENDER_EXTERNAL_URL:
        webhook_url = f"https://{RENDER_EXTERNAL_URL}/webhook"
        await application.bot.set_webhook(url=webhook_url)
        logging.info(f"Webhook configurado en {webhook_url}")

def ensure_bot():
    """Asegura que el bot esté inicializado (thread-safe)."""
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
    """Recibe y procesa las actualizaciones de Telegram vía webhook."""
    logging.debug("Webhook recibido")
    if not ensure_bot():
        return "Bot not ready", 503
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(application.process_update(update), _bot_loop)
    return "OK", 200

@app.route("/")
@app.route("/health")
def health():
    """Endpoint de salud para el deploy."""
    if not _bot_ready:
        logging.warning("Health check: bot no listo")
        return "Bot not ready", 503
    logging.debug("Health check OK")
    return "OK", 200

if __name__ == "__main__":
    logging.info(f"Iniciando servidor en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT)
