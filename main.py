import os
import time
import asyncio
import threading
import tempfile
import logging
import concurrent.futures
import traceback
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify
from telegram import Update, InputMediaPhoto, Message
from telegram.constants import ChatAction
from telegram.error import TimedOut as TelegramTimedOut, NetworkError as TelegramNetworkError
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
import yt_dlp
import requests

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# Configuracion desde variables de entorno
# ============================================================
TOKEN: Optional[str] = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN no esta configurado en las variables de entorno")

WEBHOOK_SECRET: Optional[str] = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

PORT: int = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL: Optional[str] = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
ADMIN_IDS: list[int] = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

ALLOWED_DOMAINS: list[str] = ["tiktok.com", "twitter.com", "x.com", "facebook.com", "fb.com", "reddit.com", "redd.it"]
COOKIES_FILE: str = os.environ.get("COOKIES_FILE") or os.path.join(tempfile.gettempdir(), "cookies.txt")
CACHE_DIR: str = os.environ.get("YDL_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "ydl_cache")
MAX_URLS_PER_MESSAGE: int = int(os.environ.get("MAX_URLS_PER_MESSAGE", 20))

# ============================================================
# Constantes de timeout (evita valores dispersos en el codigo)
# ============================================================
HTTP_SHORT_TIMEOUT: int = 15
HTTP_MEDIUM_TIMEOUT: int = 30
HTTP_LONG_TIMEOUT: int = 60
HTTP_DOWNLOAD_TIMEOUT: int = 120
SOCKET_TIMEOUT: int = 120
QUEUE_IDLE_TIMEOUT: int = 300          # 5 minutos
WORKER_RETRY_DELAY_BASE: int = 2       # segundos base para exponential backoff
USER_COOLDOWN_SECONDS: float = 15.0    # cooldown minimo entre batches por usuario
TG_READ_TIMEOUT: int = 120
TG_WRITE_TIMEOUT: int = 120
TG_CONNECT_TIMEOUT: int = 30
TG_MEDIA_WRITE_TIMEOUT: int = 300   # uploads grandes (hasta 50MB) necesitan mas margen

# ============================================================
# Estadisticas globales (thread-safe)
# ============================================================
_stats: dict = {
    "start_time": time.time(),
    "total_requests": 0,
    "successful": 0,
    "failed": 0,
    "unique_users": set(),  # set interno, nunca se serializa como JSON
}
_stats_lock: threading.Lock = threading.Lock()

def _inc_stats(key: str) -> None:
    """Incrementa una estadistica numerica de forma thread-safe."""
    with _stats_lock:
        _stats[key] += 1

def _add_unique_user(user_id: int) -> None:
    """Registra un usuario unico de forma thread-safe."""
    with _stats_lock:
        _stats["unique_users"].add(user_id)

# ============================================================
# Rate limiting por usuario
# ============================================================
_user_last_request: dict[int, float] = {}
_user_cooldown_lock: threading.Lock = threading.Lock()

def _check_cooldown(user_id: int) -> float:
    """Retorna los segundos restantes de cooldown, o 0 si puede proceder."""
    with _user_cooldown_lock:
        now = time.time()
        last = _user_last_request.get(user_id, 0)
        remaining = USER_COOLDOWN_SECONDS - (now - last)
        if remaining > 0:
            return remaining
        _user_last_request[user_id] = now
        return 0

# ============================================================
# Chat action throttled (send_chat_action)
# ============================================================
# Telegram solo mantiene el estado "typing/subiendo" ~5s por request.
# Llamarlo sin control quema requests de la API sin beneficio para el usuario.
_chat_action_last_sent: dict[int, float] = {}
_chat_action_lock: threading.Lock = threading.Lock()
CHAT_ACTION_INTERVAL: float = 4.5

async def _send_chat_action(bot, chat_id: int, action) -> None:
    """Envia send_chat_action con throttle por chat (max 1 cada ~4.5s)."""
    with _chat_action_lock:
        now = time.time()
        last = _chat_action_last_sent.get(chat_id, 0)
        if now - last < CHAT_ACTION_INTERVAL:
            return
        _chat_action_last_sent[chat_id] = now
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except (TelegramTimedOut, TelegramNetworkError):
        pass

# ============================================================
# Cola de descargas por usuario (FIFO)
# ============================================================
@dataclass
class DownloadTask:
    """Representa una tarea de descarga encolada por un usuario."""
    url: str
    chat_id: int
    user_id: int
    message_id: int
    bot_username: str
    processing_msg_id: Optional[int]

_user_queues: dict[int, asyncio.Queue] = {}
_queue_workers: dict[int, asyncio.Task] = {}
_queues_lock: threading.Lock = threading.Lock()

# ============================================================
# Inicializacion de Flask y Telegram
# ============================================================
app: Flask = Flask(__name__)

_download_executor: concurrent.futures.ThreadPoolExecutor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="download"
)

_tg_request: HTTPXRequest = HTTPXRequest(
    connect_timeout=TG_CONNECT_TIMEOUT,
    read_timeout=TG_READ_TIMEOUT,
    write_timeout=TG_WRITE_TIMEOUT,
    media_write_timeout=TG_MEDIA_WRITE_TIMEOUT,
    pool_timeout=30,
    connection_pool_size=256,
)

application: Application = Application.builder().token(TOKEN).request(_tg_request).build()

# ============================================================
# Handlers de comandos
# ============================================================

async def start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Mensaje de bienvenida con las plataformas soportadas y el sistema de cola."""
    user = update.effective_user
    logger.info(f"Comando /start de {user.id} (@{user.username})")
    text = (
        "\U0001f44b \u00a1Hola! Soy tu bot de descargas.\n\n"
        "\U0001f4ce **Enviame un enlace** de:\n"
        "\u2022 TikTok (sin marca de agua)\n"
        "\u2022 Facebook (videos / Reels)\n"
        "\u2022 Twitter / X (Videos / GIF)\n"
        "\u2022 Reddit (videos, imagenes y GIFs)\n\n"
        "\U0001f4e6 **Cola por usuario:**\n"
        "Puedes enviar varios enlaces seguidos. Se procesaran en orden.\n\n"
        "\u26a0\ufe0f Limite: 50 MB por archivo."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def stats(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra estadisticas del bot. Solo accesible para administradores."""
    user = update.effective_user
    if not ADMIN_IDS or user.id not in ADMIN_IDS:
        logger.warning(f"Acceso denegado a /stats por {user.id}")
        await update.message.reply_text("\u274c No tienes permiso para usar este comando.")
        return

    with _stats_lock:
        uptime: int = int(time.time() - _stats["start_time"])
        days, remainder = divmod(uptime, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime_str: str = f"{days}d {hours}h {minutes}m"

        text: str = (
            f"\U0001f4ca **Estadisticas del Bot**\n\n"
            f"\U0001f550 **Activo:** {uptime_str}\n"
            f"\U0001f4e5 **Solicitudes totales:** {_stats['total_requests']}\n"
            f"\u2705 **Exitosas:** {_stats['successful']}\n"
            f"\u274c **Fallidas:** {_stats['failed']}\n"
            f"\U0001f465 **Usuarios unicos:** {len(_stats['unique_users'])}\n"
            f"\U0001f4e6 **Colas activas:** {len(_user_queues)}\n"
        )

    await update.message.reply_text(text, parse_mode="Markdown")

# ============================================================
# Opciones de yt-dlp
# ============================================================

def get_ydl_opts() -> dict:
    """
    Retorna las opciones de configuracion para yt-dlp.
    Incluye cache de extractores para evitar re-descargar info de URLs repetidas.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    opts: dict = {
        # best[filesize<50M] primero: formato combinado con audio.
        # Luego bestvideo+bestaudio (merge DASH), luego fallback a cualquier best.
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/bestvideo+bestaudio/best",
        # Preferir codecs compatibles con WhatsApp y la mayoria de reproductores:
        # H.264 (vcodec) + AAC (acodec) en contenedor MP4.
        # Si no hay H.264 disponible, cae a lo que haya.
        "format_sort": ["vcodec:h264", "acodec:aac", "hasaud", "res", "fps"],
        "merge_output_format": "mp4",
        "socket_timeout": SOCKET_TIMEOUT,
        "extractor_retries": 3,
        "file_access_retries": 3,
        "retries": 5,
        "retry_sleep": {"extractor": "linear=1:5"},
        "concurrent_fragments": 3,
        "check_formats": True,
        "ratelimit": 10 * 1024 * 1024,
        "noplaylist": True,
        # Truncar el titulo por BYTES (no caracteres): un titulo CJK de 180
        # caracteres = ~540 bytes y excede el limite de 255 bytes del filesystem
        # (ENAMETOOLONG). 100 bytes + sufijo queda siempre por debajo de 255.
        "outtmpl": "%(title).100B [%(id)s].%(ext)s",
        "trim_filenames": 180,
        # Anti-429: pausa de 1s entre requests de info para Twitter/TikTok/etc.
        "sleep_interval_requests": 1,
        # Impersonate solo para el extractor generico (Facebook links via Cloudflare).
        # Se deja OFF globalmente: forzarlo en todo reduce velocidad y estabilidad.
        "extractor_args": {
            "generic": {"impersonate": ["chrome"]},
            "tiktok": {
                "app_version": ["35.1.3"],
                "manifest_app_version": ["2023501030"],
                "app_name": ["musical_ly"],
            },
        },
        "embedthumbnail": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": CACHE_DIR,
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
        logger.info(f"Usando cookies desde {COOKIES_FILE}")
    return opts

# ============================================================
# Fallback para TikTok Slideshows
# ============================================================

def _resolve_tiktok_url(url: str) -> str:
    """
    Resuelve URLs acortadas de TikTok (vt.tiktok.com) a su forma completa
    (www.tiktok.com/@user/video/...). Retorna la URL resuelta (sin query params)
    o la URL original limpia si falla.
    """
    parsed = urlparse(url)
    if parsed.hostname and parsed.hostname.replace("www.", "") == "vt.tiktok.com":
        try:
            head = requests.head(
                url, allow_redirects=True, timeout=HTTP_SHORT_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resolved: str = head.url.split("?")[0]
            logger.info(f"URL TikTok resuelta: {resolved}")
            return resolved
        except Exception as e:
            logger.warning(f"No se pudo resolver URL TikTok corta: {e}")
    return url.split("?")[0]


def _tiktok_api_fallback(url: str) -> Optional[tuple]:
    """
    Fallback para TikTok slideshows usando la API de tikwm.com
    cuando yt-dlp no detecta el slideshow correctamente.
    """
    logger.info(f"Usando fallback tikwm.com para {url}")
    headers: dict = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    api_url: str = _resolve_tiktok_url(url)
    resp = requests.post(
        "https://tikwm.com/api/",
        data={"url": api_url},
        headers=headers,
        timeout=HTTP_MEDIUM_TIMEOUT,
    )
    data: dict = resp.json()
    if data.get("code") != 0:
        logger.warning(f"tikwm.com respondio con codigo {data.get('code')}")
        return None
    result: dict = data.get("data", {})
    images: list = result.get("images", [])
    music_url: str = result.get("music_info", {}).get("play", "")
    if not images:
        logger.warning("tikwm.com no devolvio imagenes")
        return None
    logger.info(f"tikwm.com devolvio {len(images)} imagenes")
    slideshow_formats: list[dict] = [{"url": img, "ext": "jpg"} for img in images]
    audio_formats: list[dict] = []
    if music_url:
        audio_formats = [{"url": music_url, "ext": "mp3"}]
    return (slideshow_formats, audio_formats, images)


def _tiktok_video_api_fallback(url: str) -> Optional[str]:
    """
    Fallback para videos de TikTok (contenido sensible / age-restricted)
    usando la API de tikwm.com. Descarga el video directamente y
    retorna la ruta del archivo, o None si falla.
    """
    logger.info(f"Usando fallback tikwm.com para video TikTok: {url}")
    headers: dict = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    try:
        api_url: str = _resolve_tiktok_url(url)

        resp = requests.post(
            "https://tikwm.com/api/",
            data={"url": api_url},
            headers=headers,
            timeout=HTTP_MEDIUM_TIMEOUT,
        )
        data: dict = resp.json()
        if data.get("code") != 0:
            logger.warning(f"tikwm.com respondio con codigo {data.get('code')}: {data.get('msg', '')}")
            return None

        result: dict = data.get("data", {})
        if result.get("images"):
            logger.warning(
                "tikwm.com devolvio un slideshow, no un video — se omite el fallback"
            )
            return None
        video_url: Optional[str] = (
            result.get("play")
            or result.get("video")
            or result.get("wmplay")
            or result.get("video_with_watermark")
        )
        if not video_url:
            logger.warning(f"tikwm.com no devolvio URL de video. Keys disponibles: {list(result.keys())}")
            return None

        r = requests.get(video_url, timeout=HTTP_DOWNLOAD_TIMEOUT)
        r.raise_for_status()
        filename: str = os.path.join(
            tempfile.gettempdir(),
            f"tiktok_video_{int(time.time())}.mp4",
        )
        with open(filename, "wb") as f:
            f.write(r.content)
        logger.info(f"Video TikTok descargado via tikwm.com: {filename} ({len(r.content)} bytes)")
        return filename
    except Exception as e:
        logger.warning(f"Error en TikTok video fallback: {e}")
        return None


# ============================================================
# Fallback para Reddit: descarga directa de imagenes/GIFs
# ============================================================

def _resolve_reddit_url(url: str) -> str:
    """
    Resuelve URLs de Reddit acortadas (/s/) a su forma canonica (/comments/).
    Retorna la URL limpia (sin query params) o la original si falla.
    """
    clean: str = url.split("?")[0]
    if "/s/" in clean:
        try:
            head = requests.head(
                clean, allow_redirects=True, timeout=HTTP_SHORT_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            if head.url and "/comments/" in head.url:
                resolved: str = head.url.split("?")[0]
                logger.info(f"URL Reddit /s/ resuelta: {resolved}")
                return resolved
        except Exception:
            logger.debug("No se pudo resolver /s/ — se usara la URL original")
    return clean


def _get_reddit_media_url(post_url: str) -> Optional[str]:
    """
    Obtiene la URL directa del recurso multimedia (imagen/GIF) de un post de Reddit.
    Estrategias: yt-dlp -> oembed API -> JSON API directa.
    """
    resolved_url: str = _resolve_reddit_url(post_url)

    # Estrategia 1: yt-dlp
    try:
        with yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True,
            "socket_timeout": 20, "impersonate": "",
        }) as ydl:
            info: dict = ydl.extract_info(resolved_url, download=False, process=False)
            media_url: Optional[str] = info.get("url")
            if media_url and any(
                d in media_url for d in
                ["i.redd.it", "i.reddituploads.com", "preview.redd.it"]
            ):
                logger.info(f"Reddit URL obtenida via yt-dlp: {media_url}")
                return media_url
    except Exception:
        logger.debug("Estrategia 1 (yt-dlp) fallo")

    # Estrategia 2: oembed API
    try:
        oembed_url: str = f"https://www.reddit.com/oembed?url={resolved_url}&format=json"
        resp = requests.get(
            oembed_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=HTTP_SHORT_TIMEOUT,
        )
        if resp.status_code == 200:
            data: dict = resp.json()
            candidate: Optional[str] = data.get("thumbnail_url") or data.get("url")
            if candidate:
                logger.info(f"Reddit URL obtenida via oembed: {candidate}")
                return candidate
    except Exception:
        logger.debug("Estrategia 2 (oembed) fallo")

    # Estrategia 3: API JSON directa
    try:
        match = re.search(r"/comments/([^/]+)", resolved_url)
        if match:
            post_id: str = match.group(1)
            sub_match = re.search(r"/r/([^/]+)", resolved_url)
            sub: str = sub_match.group(1) if sub_match else ""
            api_url: str = f"https://www.reddit.com/r/{sub}/comments/{post_id}.json"
            resp = requests.get(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                timeout=HTTP_SHORT_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                post_data: dict = data[0]["data"]["children"][0]["data"]
                media_url = post_data.get("url")
                if media_url:
                    logger.info(f"Reddit URL obtenida via JSON API: {media_url}")
                    return media_url
    except Exception:
        logger.debug("Estrategia 3 (JSON API) fallo")

    return None


def _download_reddit_media(media_url: str, post_id: str) -> Optional[str]:
    """
    Descarga un archivo multimedia (imagen/GIF) desde una URL directa,
    detecta la extension real por Content-Type y lo guarda en tempfile.
    Retorna la ruta del archivo o None si falla.
    """
    try:
        try:
            head = requests.head(
                media_url, allow_redirects=True, timeout=HTTP_SHORT_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            final_url: str = head.url
        except Exception:
            final_url = media_url

        ext: str = "jpg"
        try:
            resp = requests.head(
                final_url, timeout=HTTP_SHORT_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            ct: str = resp.headers.get("Content-Type", "")
            if "gif" in ct:
                ext = "gif"
            elif "png" in ct:
                ext = "png"
            elif "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "webp" in ct:
                ext = "webp"
            else:
                path: str = urlparse(final_url).path
                ext = path.split(".")[-1].lower() if "." in path else "jpg"
        except Exception:
            path = urlparse(final_url).path
            ext = path.split(".")[-1].lower() if "." in path else "jpg"

        if ext not in ("jpg", "jpeg", "png", "gif", "webp"):
            logger.warning(f"Reddit download: extension no soportada {ext}")
            return None

        logger.info(f"Reddit download: descargando {final_url} (ext={ext})")

        r = requests.get(
            final_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=HTTP_LONG_TIMEOUT,
        )
        r.raise_for_status()

        filename: str = os.path.join(
            tempfile.gettempdir(),
            f"reddit_img_{post_id}.{ext}",
        )
        with open(filename, "wb") as f:
            f.write(r.content)

        logger.info(f"Reddit download: {filename} ({len(r.content)} bytes)")
        return filename

    except Exception as e:
        logger.warning(f"Reddit download: error {e}")
        return None


def _reddit_image_fallback(url: str) -> Optional[str]:
    """
    Fallback para posts de Reddit que contienen imagenes o GIFs en lugar de videos.
    """
    logger.info(f"Usando fallback de imagen Reddit para {url}")
    try:
        resolved: str = _resolve_reddit_url(url)
        media_url: Optional[str] = _get_reddit_media_url(resolved)
        if not media_url:
            logger.warning("Reddit fallback: no se pudo obtener la URL del recurso")
            return None

        post_id_match = re.search(r"/comments/([^/]+)", resolved)
        post_id: str = post_id_match.group(1) if post_id_match else str(int(time.time()))

        return _download_reddit_media(media_url, post_id)

    except Exception as e:
        logger.warning(f"Reddit fallback: error {e}")
        return None


# ============================================================
# Handler principal: recibe URLs y las encola
# ============================================================

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Valida la(s) URL(s) enviada(s) por el usuario y las encola para procesarlas.
    Soporta multiples URLs en un solo mensaje (una por linea).
    Se procesaran en orden FIFO por usuario.
    """
    if not update.message or not update.message.text:
        logger.warning(f"Update ignorado — sin message.text: type={update.update_id}")
        return

    raw_text: str = update.message.text.strip()
    user = update.effective_user
    chat_id: int = update.effective_chat.id

    # Rate limiting por usuario
    remaining: float = _check_cooldown(user.id)
    if remaining > 0:
        await update.message.reply_text(
            f"\u23f3 Espera **{remaining:.0f}s** antes de enviar mas enlaces.",
            parse_mode="Markdown",
        )
        return

    candidate_urls: list[str] = [line.strip() for line in raw_text.replace("\r\n", "\n").split("\n") if line.strip()]
    logger.info(f"{len(candidate_urls)} URL(s) recibida(s) de {user.id}")

    if len(candidate_urls) > MAX_URLS_PER_MESSAGE:
        logger.warning(f"Exceso de URLs de {user.id}: {len(candidate_urls)} (max {MAX_URLS_PER_MESSAGE})")
        await update.message.reply_text(
            f"\u274c Maximo **{MAX_URLS_PER_MESSAGE} enlaces** por mensaje.\n"
            f"Enviaste {len(candidate_urls)}. Dividi en varios mensajes.",
            parse_mode="Markdown",
        )
        return

    valid_urls: list[str] = []
    for url in candidate_urls:
        if not url.startswith(("http://", "https://")):
            logger.warning(f"URL invalida de {user.id}: {url}")
            continue
        if not any(domain in url.lower() for domain in ALLOWED_DOMAINS):
            logger.warning(f"Dominio no permitido de {user.id}: {url}")
            continue
        valid_urls.append(url)

    if not valid_urls:
        await update.message.reply_text(
            "\u274c No encontre enlaces validos para descargar.\n"
            "Acepto URLs de: TikTok, Facebook, Twitter/X y Reddit."
        )
        return

    for _ in valid_urls:
        _inc_stats("total_requests")
    _add_unique_user(user.id)

    if user.id not in _user_queues:
        _user_queues[user.id] = asyncio.Queue()

    if len(valid_urls) == 1:
        url: str = valid_urls[0]
        processing_msg: Message = await update.message.reply_text("\u23f3 En cola...")
        task: DownloadTask = DownloadTask(
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
            f"\u23f3 **{len(valid_urls)} enlaces encolados.**\n"
            "Se procesaran uno por uno en orden.",
            parse_mode="Markdown",
        )
        for url in valid_urls:
            task = DownloadTask(
                url=url,
                chat_id=chat_id,
                user_id=user.id,
                message_id=update.message.message_id,
                bot_username=context.bot.username,
                processing_msg_id=None,
            )
            await _user_queues[user.id].put(task)

    if user.id not in _queue_workers or _queue_workers[user.id].done():
        _queue_workers[user.id] = asyncio.create_task(_queue_worker(user.id))
        logger.info(f"Worker iniciado para usuario {user.id}")

    logger.info(f"{len(valid_urls)} tarea(s) encolada(s) para {user.id}")

# ============================================================
# Worker de cola por usuario
# ============================================================

async def _queue_worker(user_id: int) -> None:
    """
    Worker que procesa las descargas de un usuario en orden FIFO.
    Se mantiene vivo mientras haya tareas pendientes.
    Si la cola esta vacia por QUEUE_IDLE_TIMEOUT, finaliza para liberar recursos.
    """
    queue = _user_queues.get(user_id)
    if not queue:
        return

    logger.info(f"Worker activo para usuario {user_id}")

    while True:
        try:
            task: DownloadTask = await asyncio.wait_for(queue.get(), timeout=QUEUE_IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            # Limpiar solo si la cola sigue siendo la misma y esta vacia
            # (evita condicion de carrera: otro request pudo encolar mientras tanto)
            current_queue = _user_queues.get(user_id)
            if current_queue is queue and queue.empty():
                with _queues_lock:
                    if _user_queues.get(user_id) is queue and queue.empty():
                        _user_queues.pop(user_id, None)
                        _queue_workers.pop(user_id, None)
                        logger.info(f"Worker finalizado para usuario {user_id} (inactivo)")
                        return
            continue

        try:
            await _execute_download(task)
        except yt_dlp.utils.DownloadError as e:
            err_msg: str = str(e) or "(sin mensaje)"
            logger.error(f"DownloadError para {task.url}: {err_msg}")
            logger.debug(traceback.format_exc())

            url_lower: str = task.url.lower()
            friendly: dict[str, str] = {
                "No video could be found in this tweet": (
                    "\u274c No se pudo encontrar un video en ese tweet.\n"
                    "Asegurate de que el tweet contiene un video nativo de X (no un enlace externo)."
                ),
                "Requested format is not available": (
                    "\u274c No hay un formato de video disponible para este enlace."
                ),
                "This video is only available for registered users": (
                    "\u274c Este video requiere inicio de sesion en la plataforma."
                ),
                "may not be comfortable for some audiences": (
                    "\u274c Este video fue marcado como **sensible** por TikTok.\n"
                    "No es posible descargarlo sin iniciar sesion."
                ),
                "Unexpected response from webpage request": (
                    "\u274c TikTok cambio algo en su sitio y el bot no puede descargar este video por ahora.\n"
                    "Ya se reporto el problema. Proba de nuevo mas tarde."
                ),
                "Unsupported URL": (
                    "\u274c Ese enlace de Reddit no contiene un video.\n"
                    "Solo puedo descargar posts de Reddit que tengan videos (v.redd.it) "
                    "o imagenes/GIFs individuales."
                ),
            }
            display_msg: str = f"\u274c Error de descarga:\n`{err_msg[:200]}`"
            for key, msg in friendly.items():
                if key in err_msg:
                    display_msg = msg
                    break

            if not err_msg.strip() or err_msg.strip() == "(sin mensaje)":
                if "tiktok.com" in url_lower:
                    display_msg = (
                        "\u274c No se pudo descargar ese video de TikTok.\n"
                        "Puede ser un video privado, requerir inicio de sesion, "
                        "o TikTok cambio algo en su sitio. Proba de nuevo mas tarde."
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
            msg: str = str(e)[:200] or "(sin mensaje)"
            logger.error(f"Error inesperado para {task.url}: {msg}")
            logger.debug(traceback.format_exc())

            display_msg = f"\u274c Error inesperado:\n`{msg}`"

            if not str(e).strip():
                url_lower = task.url.lower()
                if "tiktok.com" in url_lower:
                    display_msg = (
                        "\u274c No se pudo descargar ese video de TikTok.\n"
                        "Puede ser un video privado, requerir inicio de sesion, "
                        "o TikTok cambio algo en su sitio. Proba de nuevo mas tarde."
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
# Helper: reintentar envio a Telegram con backoff
# ============================================================

async def _send_file_with_retry(bot, filename: str, send_factory, max_retries: int = 3):
    """
    Abre `filename` en modo 'rb', ejecuta `send_factory(file_obj)` y reintenta
    si falla con TimedOut o NetworkError. Cada reintento re-abre el archivo
    para evitar problemas con el puntero de lectura.
    """
    last_exc = None
    for attempt in range(max_retries):
        try:
            with open(filename, "rb") as f:
                return await send_factory(f)
        except (TelegramTimedOut, TelegramNetworkError) as e:
            last_exc = e
            delay: float = WORKER_RETRY_DELAY_BASE * (2 ** attempt)
            logger.warning(f"Reintento {attempt+1}/{max_retries} en {delay}s: {e}")
            await asyncio.sleep(delay)
        except Exception:
            raise
    logger.error(f"Se agotaron los reintentos para enviar {filename}: {last_exc}")
    raise last_exc


# ============================================================
# Ejecucion real de la descarga
# ============================================================

async def _execute_download(task: DownloadTask) -> None:
    """
    Ejecuta la descarga real: detecta TikTok slideshows,
    descarga con yt-dlp, sube a Telegram y limpia los archivos temporales.
    """
    url: str = task.url
    bot = application.bot
    is_tiktok: bool = "tiktok.com" in url

    # Limpiar URL de Reddit: eliminar parametros share que interfieren con yt-dlp
    if any(d in url for d in ["reddit.com", "redd.it"]):
        url = url.split("?")[0]
        if "/s/" in url:
            try:
                head = requests.head(url, allow_redirects=True, timeout=HTTP_SHORT_TIMEOUT,
                                     headers={"User-Agent": "Mozilla/5.0"})
                if head.url and "/comments/" in head.url:
                    url = head.url.split("?")[0]
                    logger.info(f"URL de Reddit resuelta: {url}")
            except Exception:
                pass

    # Si no hay mensaje de progreso (multiples URLs), crear uno ahora
    if task.processing_msg_id is None:
        try:
            msg: Message = await bot.send_message(
                chat_id=task.chat_id,
                text="\u23f3",
                reply_to_message_id=task.message_id,
            )
            task.processing_msg_id = msg.message_id
        except Exception:
            pass
    else:
        try:
            await bot.edit_message_text(
                chat_id=task.chat_id,
                message_id=task.processing_msg_id,
                text="\u23f3",
            )
        except Exception:
            pass

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    # ================================================================
    # Deteccion de TikTok slideshows / posts de una sola imagen
    # ================================================================
    if is_tiktok:
        logger.info(f"URL de TikTok detectada: {url}")

        # tikwm.com detecta correctamente tanto /photo/ como posts
        # de una sola imagen con /video/ en la URL (que yt-dlp no detecta).
        # Es un solo HTTP POST, mucho mas rapido que yt-dlp extract_info.
        api_data: Optional[tuple] = None
        try:
            api_data = await loop.run_in_executor(
                _download_executor, _tiktok_api_fallback, url
            )
        except Exception as e:
            logger.warning(f"tikwm check fallo: {e}")

        if api_data:
            slideshow_formats, _audio_formats, api_images = api_data
            logger.info(
                f"Slideshow detectado via tikwm: {len(slideshow_formats)} imagenes"
            )

            def dl_slideshow() -> list[str]:
                logger.debug("dl_slideshow: descargando imagenes")
                paths: list[str] = []
                targets: list = (
                    api_images
                    if api_images
                    else [sf.get("url") for sf in slideshow_formats]
                )
                dl_headers: dict = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.tiktok.com/",
                }
                for i, img_url in enumerate(targets):
                    if not img_url:
                        continue
                    path: str = os.path.join(
                        tempfile.gettempdir(),
                        f"tiktok_slide_{int(time.time())}_{i}.jpg",
                    )
                    for attempt in range(1, 4):
                        try:
                            r = requests.get(
                                img_url, headers=dl_headers,
                                timeout=HTTP_LONG_TIMEOUT,
                            )
                            r.raise_for_status()
                            with open(path, "wb") as f:
                                f.write(r.content)
                            paths.append(path)
                            break
                        except Exception as e:
                            logger.warning(
                                f"Imagen {i} intento {attempt}/3 fallo: {e}"
                            )
                            if attempt < 3:
                                time.sleep(1)
                return paths

            img_paths: list[str] = await loop.run_in_executor(_download_executor, dl_slideshow)
            if not img_paths:
                await bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.processing_msg_id,
                    text="\u274c No se pudieron descargar las imagenes del slideshow.",
                )
                _inc_stats("failed")
                return

            await bot.send_chat_action(
                chat_id=task.chat_id, action=ChatAction.UPLOAD_PHOTO
            )
            caption_text: str = f"\U0001f4e5 Descargado por @{task.bot_username}"
            for batch_start in range(0, len(img_paths), 10):
                batch: list[str] = img_paths[batch_start : batch_start + 10]
                media_group: list[InputMediaPhoto] = []
                files: list = []
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

            for p in img_paths:
                try:
                    os.remove(p)
                except OSError:
                    pass

            try:
                await bot.delete_message(
                    chat_id=task.chat_id, message_id=task.processing_msg_id
                )
            except Exception:
                pass

            _inc_stats("successful")
            return

    # ================================================================
    # Descarga normal con yt-dlp
    # ================================================================
    logger.info(f"Iniciando descarga yt-dlp para: {url}")

    def download() -> tuple[str, int, bool]:
        """Funcion bloqueante que corre en el executor para no bloquear el event loop."""
        logger.debug("download: iniciando yt-dlp")
        opts: dict = get_ydl_opts()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info: dict = ydl.extract_info(url, download=True)
            requested = info.get("requested_downloads")
            if requested:
                filename: str = requested[0].get("filepath", ydl.prepare_filename(info))
            else:
                filename = ydl.prepare_filename(info)
            duration: int = info.get("duration", 0)
            is_video: bool = info.get("is_video", True) or bool(info.get("duration"))
            if not os.path.exists(filename):
                base: str = os.path.splitext(filename)[0]
                for ext in [".mp4", ".webm", ".mkv", ".jpg", ".png", ".webp"]:
                    candidate: str = base + ext
                    if os.path.exists(candidate):
                        filename = candidate
                        break
            return filename, duration, is_video

    is_reddit: bool = any(d in url for d in ["reddit.com", "redd.it"])
    downloaded: bool = False
    result: Optional[tuple] = None

    # Para Reddit: intentar descarga normal, si falla probar fallback para imagenes/GIFs
    if is_reddit:
        try:
            result = await loop.run_in_executor(_download_executor, download)
            downloaded = True
        except Exception as e:
            err_text: str = str(e)
            logger.info(f"Reddit: descarga normal fallo: {err_text[:200]}")

            img_filename: Optional[str] = None
            unsupported_urls: list[str] = re.findall(r'https?://[^\s"\']+', err_text)
            for u in unsupported_urls:
                u_clean: str = u.rstrip(".,:;")
                parsed = urlparse(u_clean)
                if "reddit.com/media" in u_clean and parsed.query:
                    params: dict = parse_qs(parsed.query)
                    if "url" in params:
                        media_url: str = params["url"][0]
                        logger.info(f"Reddit: URL extraida del error: {media_url}")
                        img_filename = await loop.run_in_executor(
                            _download_executor, _download_reddit_media,
                            media_url, str(int(time.time()))
                        )
                        if img_filename:
                            logger.info(f"Reddit: descarga directa exitosa: {img_filename}")
                            break
                elif any(d in u_clean for d in ["i.redd.it", "i.reddituploads.com", "preview.redd.it"]):
                    logger.info(f"Reddit: URL directa extraida del error: {u_clean}")
                    img_filename = await loop.run_in_executor(
                        _download_executor, _download_reddit_media,
                        u_clean, str(int(time.time()))
                    )
                    if img_filename:
                        break

            if not img_filename:
                img_filename = await loop.run_in_executor(
                    _download_executor, _reddit_image_fallback, url
                )
            if img_filename:
                file_size: int = os.path.getsize(img_filename)
                if file_size > 50 * 1024 * 1024:
                    try:
                        os.remove(img_filename)
                    except OSError:
                        pass
                    await bot.edit_message_text(
                        chat_id=task.chat_id, message_id=task.processing_msg_id,
                        text="\u274c El archivo pesa mas de 50 MB."
                    )
                    _inc_stats("failed")
                    return

                ext: str = os.path.splitext(img_filename)[1].lower()
                caption: str = f"\U0001f4e5 Descargado por @{task.bot_username}"
                if ext == ".gif":
                    await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_VIDEO)
                    await _send_file_with_retry(
                        bot, img_filename,
                        lambda f: bot.send_animation(
                            chat_id=task.chat_id, animation=f, caption=caption,
                            reply_to_message_id=task.message_id,
                            read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT,
                        ),
                    )
                else:
                    if file_size > 10 * 1024 * 1024:
                        try:
                            os.remove(img_filename)
                        except OSError:
                            pass
                        await bot.edit_message_text(
                            chat_id=task.chat_id, message_id=task.processing_msg_id,
                            text="\u274c La imagen pesa mas de 10 MB y Telegram no puede enviarla."
                        )
                        _inc_stats("failed")
                        return
                    await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_PHOTO)
                    await _send_file_with_retry(
                        bot, img_filename,
                        lambda f: bot.send_photo(
                            chat_id=task.chat_id, photo=f, caption=caption,
                            reply_to_message_id=task.message_id,
                            read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT,
                        ),
                    )
                try:
                    os.remove(img_filename)
                except OSError:
                    pass
                _inc_stats("successful")
                try:
                    await bot.delete_message(chat_id=task.chat_id, message_id=task.processing_msg_id)
                except Exception:
                    pass
                return
            raise

    # Para TikTok: si yt-dlp falla (contenido sensible/age-restricted),
    # probar fallback con tikwm.com
    if is_tiktok:
        try:
            result = await loop.run_in_executor(_download_executor, download)
            downloaded = True
        except Exception as e:
            err_text = str(e)
            logger.info(f"TikTok: descarga normal fallo, probando fallback tikwm.com: {err_text[:200]}")

            tiktok_file: Optional[str] = await loop.run_in_executor(
                _download_executor, _tiktok_video_api_fallback, url
            )

            if tiktok_file:
                file_size = os.path.getsize(tiktok_file)
                if file_size > 50 * 1024 * 1024:
                    try:
                        os.remove(tiktok_file)
                    except OSError:
                        pass
                    await bot.edit_message_text(
                        chat_id=task.chat_id, message_id=task.processing_msg_id,
                        text="\u274c El archivo pesa mas de 50 MB."
                    )
                    _inc_stats("failed")
                    return

                caption = f"\U0001f4e5 Descargado por @{task.bot_username}"

                # send_video funciona con y sin audio en todos los clientes (mobile incluido).
                # send_animation con MP4 causa "formato invalido" al guardar en moviles.
                await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_VIDEO)
                await _send_file_with_retry(
                    bot, tiktok_file,
                    lambda f: bot.send_video(
                        chat_id=task.chat_id, video=f, caption=caption,
                        supports_streaming=True,
                        reply_to_message_id=task.message_id,
                        read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT, connect_timeout=TG_CONNECT_TIMEOUT,
                    ),
                )

                try:
                    os.remove(tiktok_file)
                except OSError:
                    pass
                _inc_stats("successful")
                try:
                    await bot.delete_message(chat_id=task.chat_id, message_id=task.processing_msg_id)
                except Exception:
                    pass
                return

            raise

    # Twitter / Facebook (o Reddit/TikTok cuando download() tuvo exito)
    if not downloaded:
        result = await loop.run_in_executor(_download_executor, download)

    filename, duration, is_video = result
    file_size = os.path.getsize(filename)
    logger.info(f"Descarga completada: {filename} ({file_size} bytes)")

    if file_size > 50 * 1024 * 1024:
        logger.warning(f"Archivo excede 50MB: {filename}")
        try:
            os.remove(filename)
        except OSError:
            pass
        await bot.edit_message_text(
            chat_id=task.chat_id,
            message_id=task.processing_msg_id,
            text="\u274c El archivo pesa mas de 50 MB.\n"
                 "Telegram no permite enviar archivos tan grandes a traves de bots normales.",
        )
        _inc_stats("failed")
        return

    caption = f"\U0001f4e5 Descargado por @{task.bot_username}"
    file_ext: str = os.path.splitext(filename)[1].lower()

    # Solo usar send_animation para archivos .gif reales.
    # Para MP4/webm sin audio usamos send_video que funciona en todos los clientes
    # y evita "formato invalido" al guardar en moviles.
    if file_ext == ".gif":
        await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_VIDEO)
        await _send_file_with_retry(
            bot, filename,
            lambda f: bot.send_animation(
                chat_id=task.chat_id,
                animation=f,
                caption=caption,
                reply_to_message_id=task.message_id,
                read_timeout=TG_READ_TIMEOUT,
                write_timeout=TG_WRITE_TIMEOUT,
                connect_timeout=TG_CONNECT_TIMEOUT,
            ),
        )
        logger.info(f"GIF enviado: {filename}")
    elif is_video:
        await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_VIDEO)
        await _send_file_with_retry(
            bot, filename,
            lambda f: bot.send_video(
                chat_id=task.chat_id,
                video=f,
                caption=caption,
                duration=duration if duration else None,
                supports_streaming=True,
                reply_to_message_id=task.message_id,
                read_timeout=TG_READ_TIMEOUT,
                write_timeout=TG_WRITE_TIMEOUT,
                connect_timeout=TG_CONNECT_TIMEOUT,
            ),
        )
        logger.info(f"Video enviado: {filename}")
    else:
        if file_size > 10 * 1024 * 1024:
            logger.warning(f"Foto excede 10MB, no se puede enviar: {filename}")
            try:
                os.remove(filename)
            except OSError:
                pass
            await bot.edit_message_text(
                chat_id=task.chat_id,
                message_id=task.processing_msg_id,
                text="\u274c La imagen pesa mas de 10 MB y Telegram no puede enviarla.",
            )
            _inc_stats("failed")
            return
        await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_PHOTO)
        await _send_file_with_retry(
            bot, filename,
            lambda f: bot.send_photo(
                chat_id=task.chat_id,
                photo=f,
                caption=caption,
                reply_to_message_id=task.message_id,
                read_timeout=TG_READ_TIMEOUT,
                write_timeout=TG_WRITE_TIMEOUT,
                connect_timeout=TG_CONNECT_TIMEOUT,
            ),
        )
        logger.info(f"Foto enviada: {filename}")

    try:
        os.remove(filename)
    except OSError:
        pass
    _inc_stats("successful")
    logger.info(f"Descarga exitosa para: {url}")

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
_bot_loop: Optional[asyncio.AbstractEventLoop] = None
_bot_ready: bool = False
_bot_lock: threading.Lock = threading.Lock()


async def _init_bot() -> None:
    """Inicializa la aplicacion de python-telegram-bot y configura el webhook si aplica."""
    logger.info("Inicializando bot...")
    await application.initialize()
    await application.start()
    if RENDER_EXTERNAL_URL:
        webhook_url: str = f"https://{RENDER_EXTERNAL_URL}/webhook"
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message"],
            max_connections=40,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook configurado en {webhook_url}")


def _bot_loop_thread(loop: asyncio.AbstractEventLoop) -> None:
    """Thread daemon que corre el event loop unico del bot durante toda la vida del proceso."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def ensure_bot() -> bool:
    """Asegura que el bot este inicializado (thread-safe, idempotente).

    Usa SIEMPRE el mismo event loop (thread daemon persistente). Si se creara un
    loop nuevo en cada reintento, el cliente HTTPX de la Application quedaria
    atado al loop del primer intento y los siguientes fallarian con
    "bound to a different event loop".
    """
    global _bot_loop, _bot_ready
    if _bot_ready:
        return True
    with _bot_lock:
        if _bot_ready:
            return True
        try:
            if _bot_loop is None:
                loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
                _bot_loop = loop
                t: threading.Thread = threading.Thread(
                    target=_bot_loop_thread, args=(loop,), daemon=True
                )
                t.start()
            future = asyncio.run_coroutine_threadsafe(_init_bot(), _bot_loop)
            future.result(timeout=60)
            _bot_ready = True
            logger.info("Bot inicializado correctamente")
            return True
        except Exception as e:
            logger.error(f"Error inicializando bot: {e}")
            return False


# ============================================================
# Shutdown graceful
# ============================================================
# NOTA: El manejo de SIGTERM lo deja gestionar a gunicorn (worker lifecycle).
# El ThreadPoolExecutor se autolimpia en el exit del interprete via el atexit
# de concurrent.futures, y el event loop del bot corre en un thread daemon que
# muere con el proceso. Un handler propio con executor.shutdown(wait=True) + 
# os._exit(0) bloqueaba o cortaba requests en vuelo durante los restarts.

# ============================================================
# Endpoints de Flask
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe y procesa las actualizaciones de Telegram via webhook."""
    logger.debug("Webhook recibido")
    # ensure_bot() DEBE correr antes del check del secret: si el webhook en
    # Telegram es de una config vieja (sin secret), el primer update llega sin
    # el header y este bootstrap lo usa para re-configurar set_webhook() con el
    # secret. Asi Telegram reintenta ese mismo update YA con el header y pasa.
    if not ensure_bot():
        return "Bot not ready", 503
    if WEBHOOK_SECRET:
        token_header: Optional[str] = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if token_header != WEBHOOK_SECRET:
            logger.warning("Webhook rechazado: secret_token invalido")
            return "Forbidden", 403
    update: Update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run_coroutine_threadsafe(
        application.process_update(update), _bot_loop
    )
    return "OK", 200


@app.route("/")
@app.route("/health")
def health():
    """
    Endpoint de salud.
    Siempre responde 200 mientras el proceso este vivo (Render reinicia tras
    errores 5xx consecutivos, asi que no usamos 503 como "not ready").
    El estado real del bot va en el campo `bot_ready`.
    """
    # Bootstrap lazy DESDE el worker (NO en import: gunicorn corre con --preload
    # y el import/init del master se hereda roto en los workers forkeados).
    # Fire-and-forget para no bloquear el health check. ensure_bot es
    # idempotente y thread-safe, asi que varios triggers son seguros.
    if not _bot_ready:
        threading.Thread(target=ensure_bot, daemon=True).start()

    try:
        yt_ver: str = getattr(yt_dlp.version, "__version__", str(yt_dlp.version))
    except Exception:
        yt_ver = "unknown"

    health_data: dict = {
        "status": "ok" if _bot_ready else "starting",
        "yt_dlp_version": yt_ver,
        "bot_ready": _bot_ready,
        "queues": {
            "active_queues": len(_user_queues),
            "active_workers": len(_queue_workers),
        },
    }

    logger.debug("Health check OK")
    return jsonify(health_data), 200


# ============================================================
# Punto de entrada
# ============================================================

if __name__ == "__main__":
    logger.info(f"Iniciando servidor en puerto {PORT}")
    app.run(host="0.0.0.0", port=PORT)
