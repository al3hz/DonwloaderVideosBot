import os
import time
import asyncio
import threading
import tempfile
import logging
import concurrent.futures
import subprocess
import traceback
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from flask import Flask, request, jsonify
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import requests

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)

# ============================================================
# Configuración desde variables de entorno
# ============================================================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

ALLOWED_DOMAINS = ["tiktok.com", "instagram.com", "twitter.com", "x.com", "facebook.com", "fb.com", "reddit.com", "redd.it"]
COOKIES_FILE = os.environ.get("COOKIES_FILE") or os.path.join(tempfile.gettempdir(), "cookies.txt")
CACHE_DIR = os.environ.get("YDL_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "ydl_cache")
MAX_URLS_PER_MESSAGE = int(os.environ.get("MAX_URLS_PER_MESSAGE", 20))

# ============================================================
# Estadísticas globales (thread-safe)
# ============================================================
_stats = {
    "start_time": time.time(),
    "total_requests": 0,
    "successful": 0,
    "failed": 0,
    "unique_users": set(),
}
_stats_lock = threading.Lock()

def _inc_stats(key: str):
    """Incrementa una estadística numérica de forma thread-safe."""
    with _stats_lock:
        _stats[key] += 1

def _add_unique_user(user_id: int):
    """Registra un usuario único de forma thread-safe."""
    with _stats_lock:
        _stats["unique_users"].add(user_id)

# ============================================================
# Cola de descargas por usuario (FIFO)
# ============================================================
@dataclass
class DownloadTask:
    """Representa una tarea de descarga encolada por un usuario."""
    url: str
    chat_id: int
    user_id: int
    message_id: int          # ID del mensaje original del usuario para responder
    bot_username: str
    processing_msg_id: int   # ID del mensaje ⏳ para editar/eliminar

_user_queues: dict[int, asyncio.Queue] = {}     # user_id -> cola de DownloadTask
_queue_workers: dict[int, asyncio.Task] = {}    # user_id -> worker activo

# ============================================================
# Inicialización de Flask y Telegram
# ============================================================
app = Flask(__name__)

# Executor para descargas en segundo plano (máximo 2 simultáneas)
_download_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="download"
)

application = Application.builder().token(TOKEN).build()

# ============================================================
# Handlers de comandos
# ============================================================

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Mensaje de bienvenida con las plataformas soportadas y el sistema de cola."""
    user = update.effective_user
    logging.info(f"Comando /start de {user.id} (@{user.username})")
    text = (
        "👋 ¡Hola! Soy tu bot de descargas.\n\n"
        "📎 **Envíame un enlace** de:\n"
        "• TikTok (sin marca de agua)\n"
        "• Instagram (Reels)\n"
        "• Facebook (videos / Reels)\n"
        "• Twitter / X (Videos / GIF)\n"
        "• Reddit (videos, imágenes y GIFs)\n\n"
        "📦 **Cola por usuario:**\n"
        "Puedes enviar varios enlaces seguidos. Se procesarán en orden.\n\n"
        "⚠️ Límite: 50 MB por archivo."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def stats(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas del bot. Solo accesible para administradores."""
    user = update.effective_user
    if not ADMIN_IDS or user.id not in ADMIN_IDS:
        logging.warning(f"Acceso denegado a /stats por {user.id}")
        await update.message.reply_text("❌ No tienes permiso para usar este comando.")
        return

    with _stats_lock:
        uptime = int(time.time() - _stats["start_time"])
        days, remainder = divmod(uptime, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str = f"{days}d {hours}h {minutes}m"

        text = (
            f"📊 **Estadísticas del Bot**\n\n"
            f"🕐 **Activo:** {uptime_str}\n"
            f"📥 **Solicitudes totales:** {_stats['total_requests']}\n"
            f"✅ **Exitosas:** {_stats['successful']}\n"
            f"❌ **Fallidas:** {_stats['failed']}\n"
            f"👥 **Usuarios únicos:** {len(_stats['unique_users'])}\n"
            f"📦 **Colas activas:** {len(_user_queues)}\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# Opciones de yt-dlp
# ============================================================

def get_ydl_opts():
    """
    Retorna las opciones de configuración para yt-dlp.
    Incluye caché de extractores para evitar re-descargar info de URLs repetidas.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    opts = {
        "format": "bestvideo[filesize<50M]+bestaudio/best[filesize<50M]/bestvideo+bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "socket_timeout": 120,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "quiet": True,
        "no_warnings": True,
        "cachedir": CACHE_DIR,
        "impersonate": "",  # auto-seleccionar impersonación si curl_cffi está disponible
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logging.info(f"Usando cookies desde {COOKIES_FILE}")
    return opts

# ============================================================
# Fallback para TikTok Slideshows
# ============================================================

def _tiktok_api_fallback(url):
    """
    Fallback para TikTok slideshows usando la API de tikwm.com
    cuando yt-dlp no detecta el slideshow correctamente.
    """
    logging.info(f"Usando fallback tikwm.com para {url}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    resp = requests.post(
        "https://tikwm.com/api/",
        data={"url": url.split("?")[0]},
        headers=headers,
        timeout=30,
    )
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

# ============================================================
# Fallback para Reddit: descarga directa de imágenes/GIFs
# ============================================================

def _get_reddit_media_url(post_url: str) -> str | None:
    """
    Obtiene la URL directa del recurso multimedia (imagen/GIF) de un post de Reddit.
    Estrategias en orden:
    1. yt-dlp con process=False  (fallo conocido en datacenters)
    2. API oembed de Reddit       (ligera, suele funcionar)
    3. API JSON directa           (más pesada, último recurso)
    """
    clean_url = post_url.split("?")[0]

    # --- Estrategia 1: yt-dlp ---
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True,
            "socket_timeout": 20, "impersonate": "",
        }) as ydl:
            info = ydl.extract_info(clean_url, download=False, process=False)
            media_url = info.get("url")
            if media_url and any(
                d in media_url for d in
                ["i.redd.it", "i.reddituploads.com", "preview.redd.it"]
            ):
                logging.info(f"Reddit URL obtenida via yt-dlp: {media_url}")
                return media_url
    except Exception:
        logging.debug("Estrategia 1 (yt-dlp) falló")

    # --- Estrategia 2: oembed API ---
    try:
        oembed_url = f"https://www.reddit.com/oembed?url={clean_url}&format=json"
        resp = requests.get(
            oembed_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            # oembed devuelve thumbnail_url para imágenes, url para videos
            candidate = data.get("thumbnail_url") or data.get("url")
            if candidate:
                logging.info(f"Reddit URL obtenida via oembed: {candidate}")
                return candidate
    except Exception:
        logging.debug("Estrategia 2 (oembed) falló")

    # --- Estrategia 3: API JSON directa ---
    try:
        match = re.search(r"/comments/([^/]+)", clean_url)
        if match:
            post_id = match.group(1)
            sub_match = re.search(r"/r/([^/]+)", clean_url)
            sub = sub_match.group(1) if sub_match else ""
            api_url = f"https://www.reddit.com/r/{sub}/comments/{post_id}.json"
            resp = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                post_data = data[0]["data"]["children"][0]["data"]
                media_url = post_data.get("url")
                if media_url:
                    logging.info(f"Reddit URL obtenida via JSON API: {media_url}")
                    return media_url
    except Exception:
        logging.debug("Estrategia 3 (JSON API) falló")

    return None


def _download_reddit_media(media_url: str, post_id: str) -> str | None:
    """
    Descarga un archivo multimedia (imagen/GIF) desde una URL directa,
    detecta la extensión real por Content-Type y lo guarda en tempfile.
    Retorna la ruta del archivo o None si falla.
    """
    try:
        # Seguir redirecciones hasta la URL final
        try:
            head = requests.head(
                media_url, allow_redirects=True, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            final_url = head.url
        except Exception:
            final_url = media_url

        # Determinar extensión por Content-Type
        ext = "jpg"
        try:
            resp = requests.head(
                final_url, timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            ct = resp.headers.get("Content-Type", "")
            if "gif" in ct:
                ext = "gif"
            elif "png" in ct:
                ext = "png"
            elif "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "webp" in ct:
                ext = "webp"
            else:
                path = urlparse(final_url).path
                ext = path.split(".")[-1].lower() if "." in path else "jpg"
        except Exception:
            path = urlparse(final_url).path
            ext = path.split(".")[-1].lower() if "." in path else "jpg"

        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            logging.warning(f"Reddit download: extensión no soportada {ext}")
            return None

        logging.info(f"Reddit download: descargando {final_url} (ext={ext})")

        r = requests.get(
            final_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=60,
        )
        r.raise_for_status()

        filename = os.path.join(
            tempfile.gettempdir(),
            f"reddit_img_{post_id}.{ext}",
        )
        with open(filename, "wb") as f:
            f.write(r.content)

        logging.info(f"Reddit download: {filename} ({len(r.content)} bytes)")
        return filename

    except Exception as e:
        logging.warning(f"Reddit download: error {e}")
        return None


def _reddit_image_fallback(url):
    """
    Fallback para posts de Reddit que contienen imágenes o GIFs en lugar de videos.
    1. Limpia la URL (quita parámetros share)
    2. Obtiene la URL del recurso multimedia (_get_reddit_media_url)
    3. Descarga la imagen/GIF (_download_reddit_media)
    """
    logging.info(f"Usando fallback de imagen Reddit para {url}")
    try:
        clean_url = url.split("?")[0]

        # Obtener URL del recurso multimedia
        media_url = _get_reddit_media_url(clean_url)
        if not media_url:
            logging.warning("Reddit fallback: no se pudo obtener la URL del recurso")
            return None

        # Extraer un ID para el nombre del archivo
        post_id_match = re.search(r"/comments/([^/]+)", clean_url)
        post_id = post_id_match.group(1) if post_id_match else str(int(time.time()))

        return _download_reddit_media(media_url, post_id)

    except Exception as e:
        logging.warning(f"Reddit fallback: error {e}")
        return None


# ============================================================
# Handler principal: recibe URLs y las encola
# ============================================================

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Valida la(s) URL(s) enviada(s) por el usuario y las encola para procesarlas.
    Soporta múltiples URLs en un solo mensaje (una por línea).
    Se procesarán en orden FIFO por usuario.
    """
    # Proteger contra actualizaciones sin mensaje (callback_query, channel_post, etc.)
    if not update.message or not update.message.text:
        logging.warning(f"Update ignorado — sin message.text: type={update.update_id}")
        return

    raw_text = update.message.text.strip()
    user = update.effective_user
    chat_id = update.effective_chat.id

    # Separar por saltos de línea o espacios, filtrar líneas vacías
    candidate_urls = [line.strip() for line in raw_text.replace("\r\n", "\n").split("\n") if line.strip()]
    logging.info(f"{len(candidate_urls)} URL(s) recibida(s) de {user.id}")

    # Limitar cantidad de URLs por mensaje para evitar abusos
    if len(candidate_urls) > MAX_URLS_PER_MESSAGE:
        logging.warning(f"Exceso de URLs de {user.id}: {len(candidate_urls)} (máx {MAX_URLS_PER_MESSAGE})")
        await update.message.reply_text(
            f"❌ Máximo **{MAX_URLS_PER_MESSAGE} enlaces** por mensaje.\n"
            f"Enviaste {len(candidate_urls)}. Dividí en varios mensajes.",
            parse_mode="Markdown",
        )
        return

    # Validar cada URL y quedarse solo con las válidas
    valid_urls = []
    for url in candidate_urls:
        if not url.startswith(("http://", "https://")):
            logging.warning(f"URL inválida de {user.id}: {url}")
            continue
        if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
            logging.warning(f"Dominio no permitido de {user.id}: {url}")
            continue
        if "instagram.com/p/" in url:
            logging.info(f"URL de foto IG rechazada: {url}")
            continue
        valid_urls.append(url)

    if not valid_urls:
        await update.message.reply_text(
            "❌ No encontré enlaces válidos para descargar.\n"
            "Acepto URLs de: TikTok, Instagram Reels, Facebook, Twitter/X y Reddit."
        )
        return

    # Registrar métricas del usuario
    for _ in valid_urls:
        _inc_stats("total_requests")
    _add_unique_user(user.id)

    # Obtener o crear cola para este usuario
    if user.id not in _user_queues:
        _user_queues[user.id] = asyncio.Queue()

    # Avisar y encolar según cantidad de URLs
    if len(valid_urls) == 1:
        url = valid_urls[0]
        processing_msg = await update.message.reply_text("⏳ En cola...")
        task = DownloadTask(
            url=url,
            chat_id=chat_id,
            user_id=user.id,
            message_id=update.message.message_id,
            bot_username=context.bot.username,
            processing_msg_id=processing_msg.message_id,
        )
        await _user_queues[user.id].put(task)
    else:
        await update.message.reply_text(
            f"⏳ **{len(valid_urls)} enlaces encolados.**\n"
            "Se procesarán uno por uno en orden.",
            parse_mode="Markdown",
        )
        for url in valid_urls:
            # Múltiples URLs: sin mensaje individual, el worker asignará uno al empezar
            task = DownloadTask(
                url=url,
                chat_id=chat_id,
                user_id=user.id,
                message_id=update.message.message_id,
                bot_username=context.bot.username,
                processing_msg_id=None,
            )
            await _user_queues[user.id].put(task)

    # Iniciar worker si no hay uno corriendo para este usuario
    if user.id not in _queue_workers or _queue_workers[user.id].done():
        _queue_workers[user.id] = asyncio.create_task(_queue_worker(user.id))
        logging.info(f"Worker iniciado para usuario {user.id}")

    logging.info(f"{len(valid_urls)} tarea(s) encolada(s) para {user.id}")

# ============================================================
# Worker de cola por usuario
# ============================================================

async def _queue_worker(user_id: int):
    """
    Worker que procesa las descargas de un usuario en orden FIFO.
    Se mantiene vivo mientras haya tareas pendientes.
    Si la cola está vacía por 5 minutos, finaliza para liberar recursos.
    """
    queue = _user_queues.get(user_id)
    if not queue:
        return

    logging.info(f"Worker activo para usuario {user_id}")

    while True:
        try:
            # Esperar hasta 5 minutos por una nueva tarea
            task = await asyncio.wait_for(queue.get(), timeout=300)
        except asyncio.TimeoutError:
            # 5 minutos sin actividad: limpiar y salir
            if queue.empty():
                _user_queues.pop(user_id, None)
                _queue_workers.pop(user_id, None)
                logging.info(f"Worker finalizado para usuario {user_id} (inactivo)")
                return
            continue

        try:
            await _execute_download(task)
        except yt_dlp.utils.DownloadError as e:
            # Error conocido de yt-dlp: mostrar mensaje amigable
            err_msg = str(e) or "(sin mensaje)"
            logging.error(f"DownloadError para {task.url}: {err_msg}")
            logging.debug(traceback.format_exc())

            # Detectar plataforma para mensajes contextuales
            url_lower = task.url.lower()
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
                "may not be comfortable for some audiences": (
                    "❌ Este video fue marcado como **sensible** por TikTok.\n"
                    "No es posible descargarlo sin iniciar sesión."
                ),
                "Unexpected response from webpage request": (
                    "❌ TikTok cambió algo en su sitio y el bot no puede descargar este video por ahora.\n"
                    "Ya se reportó el problema. Probá de nuevo más tarde."
                ),
                "Unsupported URL": (
                    "❌ Ese enlace de Reddit no contiene un video.\n"
                    "Solo puedo descargar posts de Reddit que tengan videos (v.redd.it) "
                    "o imágenes/GIFs individuales."
                ),
            }
            display_msg = f"❌ Error de descarga:\n`{err_msg[:200]}`"
            for key, msg in friendly.items():
                if key in err_msg:
                    display_msg = msg
                    break

            # Si el mensaje está vacío o no se pudo clasificar, dar contexto según plataforma
            if not err_msg.strip() or err_msg.strip() == "(sin mensaje)":
                if "instagram.com" in url_lower:
                    display_msg = (
                        "❌ No se pudo descargar ese Reel de Instagram.\n"
                        "Puede ser un video privado, requerir inicio de sesión, "
                        "o Instagram cambió algo en su sitio. Probá de nuevo más tarde."
                    )
                elif "tiktok.com" in url_lower:
                    display_msg = (
                        "❌ No se pudo descargar ese video de TikTok.\n"
                        "Puede ser un video privado, requerir inicio de sesión, "
                        "o TikTok cambió algo en su sitio. Probá de nuevo más tarde."
                    )

            try:
                await application.bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.processing_msg_id,
                    text=display_msg,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            _inc_stats("failed")

        except Exception as e:
            # Error inesperado — loguear traceback completo
            msg = str(e)[:200] or "(sin mensaje)"
            logging.error(f"Error inesperado para {task.url}: {msg}")
            logging.debug(traceback.format_exc())

            display_msg = f"❌ Error inesperado:\n`{msg}`"

            # Si el error está vacío, dar contexto según plataforma
            if not str(e).strip():
                url_lower = task.url.lower()
                if "instagram.com" in url_lower:
                    display_msg = (
                        "❌ No se pudo descargar ese Reel de Instagram.\n"
                        "Puede ser un video privado, requerir inicio de sesión, "
                        "o Instagram cambió algo en su sitio. Probá de nuevo más tarde."
                    )
                elif "tiktok.com" in url_lower:
                    display_msg = (
                        "❌ No se pudo descargar ese video de TikTok.\n"
                        "Puede ser un video privado, requerir inicio de sesión, "
                        "o TikTok cambió algo en su sitio. Probá de nuevo más tarde."
                    )

            try:
                await application.bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.processing_msg_id,
                    text=display_msg,
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            _inc_stats("failed")

        finally:
            queue.task_done()

# ============================================================
# Ejecución real de la descarga
# ============================================================

async def _execute_download(task: DownloadTask):
    """
    Ejecuta la descarga real: detecta TikTok slideshows,
    descarga con yt-dlp, sube a Telegram y limpia los archivos temporales.
    """
    url = task.url
    bot = application.bot
    is_tiktok = "tiktok.com" in url

    # Limpiar URL de Reddit: eliminar parámetros share que interfieren con yt-dlp
    if any(d in url for d in ["reddit.com", "redd.it"]):
        url = url.split("?")[0]
        # Si es /s/, resolver la redirección a /comments/
        if "/s/" in url:
            try:
                head = requests.head(url, allow_redirects=True, timeout=15,
                                     headers={"User-Agent": "Mozilla/5.0"})
                if head.url and "/comments/" in head.url:
                    url = head.url.split("?")[0]
                    logging.info(f"URL de Reddit resuelta: {url}")
            except Exception:
                pass

    # Si no hay mensaje de progreso (múltiples URLs), crear uno ahora
    if task.processing_msg_id is None:
        try:
            msg = await bot.send_message(
                chat_id=task.chat_id,
                text="⏳",
                reply_to_message_id=task.message_id,
            )
            task.processing_msg_id = msg.message_id
        except Exception:
            pass
    else:
        # Actualizar mensaje de cola a procesando
        try:
            await bot.edit_message_text(
                chat_id=task.chat_id,
                message_id=task.processing_msg_id,
                text="⏳",
            )
        except Exception:
            pass

    loop = asyncio.get_running_loop()

    # ================================================================
    # Detección y manejo de TikTok slideshows (/photo/)
    # ================================================================
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
            slideshow_formats = [
                f for f in formats
                if f.get("format_id", "").startswith("slideshow-")
            ]
            if slideshow_formats:
                audio_formats = [
                    f for f in formats
                    if f.get("vcodec") == "none" and f.get("acodec", "none") != "none"
                ]
                slideshow_data = (slideshow_formats, audio_formats, None)
                logging.info(
                    f"Slideshow detectado via yt-dlp: {len(slideshow_formats)} slides"
                )
        except Exception as e:
            logging.warning(f"yt-dlp falló detectando slideshow: {e}")

        # Intento 2: fallback via API de tikwm.com
        if not slideshow_data:
            try:
                api_data = await loop.run_in_executor(
                    _download_executor, _tiktok_api_fallback, url
                )
                if api_data:
                    slideshow_data = api_data
            except Exception as e:
                logging.warning(f"Fallback tikwm también falló: {e}")

        # Si se detectó slideshow, descargar imágenes y enviar como álbum
        if slideshow_data:
            slideshow_formats, _audio_formats, api_images = slideshow_data
            logging.info(
                f"Procesando slideshow con {len(slideshow_formats)} imágenes"
            )

            # Descarga todas las imágenes
            def dl_slideshow():
                logging.debug("dl_slideshow: descargando imagenes")
                paths = []
                targets = (
                    api_images
                    if api_images
                    else [sf.get("url") for sf in slideshow_formats]
                )
                for i, img_url in enumerate(targets):
                    if not img_url:
                        continue
                    path = os.path.join(
                        tempfile.gettempdir(),
                        f"tiktok_slide_{int(time.time())}_{i}.jpg",
                    )
                    r = requests.get(img_url, timeout=60)
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        f.write(r.content)
                    paths.append(path)
                return paths

            img_paths = await loop.run_in_executor(_download_executor, dl_slideshow)
            if not img_paths:
                await bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.processing_msg_id,
                    text="❌ No se pudieron descargar las imágenes.",
                )
                _inc_stats("failed")
                return

            # Enviar álbum en lotes de 10
            caption_text = f"📥 Descargado por @{task.bot_username}"
            for batch_start in range(0, len(img_paths), 10):
                batch = img_paths[batch_start : batch_start + 10]
                media_group = []
                files = []
                for i, path in enumerate(batch):
                    f = open(path, "rb")
                    files.append(f)
                    if batch_start == 0 and i == 0:
                        media_group.append(
                            InputMediaPhoto(f, caption=caption_text)
                        )
                    else:
                        media_group.append(InputMediaPhoto(f))
                await bot.send_media_group(
                    chat_id=task.chat_id,
                    media=media_group,
                    reply_to_message_id=task.message_id,
                )
                for f in files:
                    f.close()

            # Limpiar archivos
            for p in img_paths:
                os.remove(p)

            try:
                await bot.delete_message(
                    chat_id=task.chat_id, message_id=task.processing_msg_id
                )
            except Exception:
                pass

            _inc_stats("successful")
            return

    # ================================================================
    # Descarga normal con yt-dlp (videos TikTok, Instagram Reels, Twitter/X, Facebook, Reddit)
    # ================================================================
    logging.info(f"Iniciando descarga yt-dlp para: {url}")

    def download():
        """Función bloqueante que corre en el executor para no bloquear el event loop."""
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

    is_reddit = any(d in url for d in ["reddit.com", "redd.it"])

    # Para Reddit: intentar descarga normal, si falla probar fallback para imágenes/GIFs
    if is_reddit:
        try:
            result = await loop.run_in_executor(_download_executor, download)
        except Exception as e:
            logging.info(f"Reddit: descarga normal falló, probando fallback de imagen: {e}")
            img_filename = await loop.run_in_executor(_download_executor, _reddit_image_fallback, url)
            if img_filename:
                file_size = os.path.getsize(img_filename)
                if file_size > 50 * 1024 * 1024:
                    os.remove(img_filename)
                    await bot.edit_message_text(
                        chat_id=task.chat_id, message_id=task.processing_msg_id,
                        text="❌ El archivo pesa más de 50 MB."
                    )
                    _inc_stats("failed")
                    return
                # Enviar como foto o GIF según extensión
                ext = os.path.splitext(img_filename)[1].lower()
                caption = f"📥 Descargado por @{task.bot_username}"
                if ext == ".gif":
                    with open(img_filename, "rb") as f:
                        await bot.send_animation(
                            chat_id=task.chat_id, animation=f, caption=caption,
                            read_timeout=120, write_timeout=120, connect_timeout=30,
                        )
                else:
                    with open(img_filename, "rb") as f:
                        await bot.send_photo(
                            chat_id=task.chat_id, photo=f, caption=caption,
                            read_timeout=120, write_timeout=120, connect_timeout=30,
                        )
                os.remove(img_filename)
                _inc_stats("successful")
                try:
                    await bot.delete_message(chat_id=task.chat_id, message_id=task.processing_msg_id)
                except Exception:
                    pass
                return
            # Si el fallback también falla, propagar el error original
            raise

    result = await loop.run_in_executor(_download_executor, download)
    filename, duration, is_video = result
    file_size = os.path.getsize(filename)
    logging.info(f"Descarga completada: {filename} ({file_size} bytes)")

    # Verificar límite de 50 MB de Telegram
    if file_size > 50 * 1024 * 1024:
        logging.warning(f"Archivo excede 50MB: {filename}")
        os.remove(filename)
        await bot.edit_message_text(
            chat_id=task.chat_id,
            message_id=task.processing_msg_id,
            text="❌ El archivo pesa más de 50 MB.\n"
                 "Telegram no permite enviar archivos tan grandes a través de bots normales.",
        )
        _inc_stats("failed")
        return

    # Detectar si el archivo tiene audio (si no, se envía como GIF animado)
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", filename,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        has_audio_stream = bool(probe.stdout.strip())
    except Exception:
        has_audio_stream = True

    caption = f"📥 Descargado por @{task.bot_username}"

    if is_video and not has_audio_stream:
        with open(filename, "rb") as f:
            await bot.send_animation(
                chat_id=task.chat_id,
                animation=f,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        logging.info(f"GIF enviado: {filename}")
    elif is_video:
        with open(filename, "rb") as f:
            await bot.send_video(
                chat_id=task.chat_id,
                video=f,
                caption=caption,
                duration=duration if duration else None,
                supports_streaming=True,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        logging.info(f"Video enviado: {filename}")
    else:
        with open(filename, "rb") as f:
            await bot.send_photo(
                chat_id=task.chat_id,
                photo=f,
                caption=caption,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        logging.info(f"Foto enviada: {filename}")

    os.remove(filename)
    _inc_stats("successful")
    logging.info(f"Descarga exitosa para: {url}")

    # Eliminar mensaje de progreso
    try:
        await bot.delete_message(
            chat_id=task.chat_id,
            message_id=task.processing_msg_id,
        )
    except Exception:
        pass

# ============================================================
# Registro de handlers de Telegram
# ============================================================
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, download_video)
)

# ============================================================
# Lifecycle del bot (thread-safe)
# ============================================================
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


# ============================================================
# Endpoints de Flask
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe y procesa las actualizaciones de Telegram vía webhook."""
    logging.debug("Webhook recibido")
    if not ensure_bot():
        return "Bot not ready", 503
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(
        application.process_update(update), _bot_loop
    )
    return "OK", 200


@app.route("/")
@app.route("/health")
def health():
    """
    Endpoint de salud mejorado.
    Retorna JSON con el estado del bot, versión de yt-dlp e info de colas activas.
    """
    if not _bot_ready:
        logging.warning("Health check: bot no listo")
        return jsonify({"status": "error", "message": "Bot not ready"}), 503

    try:
        yt_ver = getattr(yt_dlp.version, "__version__", str(yt_dlp.version))
    except Exception:
        yt_ver = "unknown"

    health_data = {
        "status": "ok",
        "yt_dlp_version": yt_ver,
        "bot_ready": _bot_ready,
        "queues": {
            "active_queues": len(_user_queues),
            "active_workers": len(_queue_workers),
        },
    }

    logging.debug("Health check OK")
    return jsonify(health_data), 200


# ============================================================
# Punto de entrada
# ============================================================

if __name__ == "__main__":
    logging.info(f"Iniciando servidor en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT)
