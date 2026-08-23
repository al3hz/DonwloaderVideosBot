import os
import time
import asyncio
import threading
import tempfile
import logging
import concurrent.futures
import traceback
import html
import json
import sys
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse, parse_qs

from flask import Flask, request, jsonify
from telegram import Update, InputMediaPhoto, Message, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import (
    BadRequest as TelegramBadRequest,
    TimedOut as TelegramTimedOut,
    NetworkError as TelegramNetworkError,
    RetryAfter as TelegramRetryAfter,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest
import yt_dlp
import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests import Response as CurlResponse

from dotenv import load_dotenv
load_dotenv()

# ============================================================
# Logging con redaccion de token
# ============================================================
# httpx (usado por python-telegram-bot) loguea cada llamada a la API como URL
# completa (.../bot<TOKEN>/sendMessage), y Render persiste esas lineas en su
# dashboard/log drains. El formatter/filtro de abajo sustituyen el token por
# 'bot<redacted>' en TODOS los mensajes y tracebacks que pasan por root.
_TOKEN_IN_URL_RE = re.compile(r"bot\d+:[A-Za-z0-9_\-]+")


def _scrub_token(text: str) -> str:
    return _TOKEN_IN_URL_RE.sub("bot<redacted>", text)


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _scrub_token(super().format(record))


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = _scrub_token(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(_RedactingFormatter("%(levelname)s:%(name)s:%(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler], force=True)

# Cintur y tirantes: ademas del handler, filtrar los loggers que mas exponen.
for _name in ("httpx", "telegram", "telegram.request", "telegram.bot"):
    logging.getLogger(_name).addFilter(_RedactingFilter())

logger = logging.getLogger(__name__)

# ============================================================
# Configuracion desde variables de entorno
# ============================================================
TOKEN: Optional[str] = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN no esta configurado en las variables de entorno")

WEBHOOK_SECRET: Optional[str] = os.environ.get("TELEGRAM_WEBHOOK_SECRET")

def _env_int(name: str, default: int) -> int:
    """Lee una variable de entorno como entero con fallback seguro.

    Si la variable no esta definida o no es un entero valido, retorna el
    default y loguea un warning (evita que un typo en Render tumbe el import).
    """
    raw: str = os.environ.get(name, "")
    if raw == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(f"Variable {name} invalida ('{raw}'), usando default {default}")
        return default


PORT: int = _env_int("PORT", 8080)
RENDER_EXTERNAL_URL: Optional[str] = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
ADMIN_IDS: list[int] = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

ALLOWED_DOMAINS: list[str] = [
    "tiktok.com", "twitter.com", "x.com", "facebook.com", "fb.com",
    "reddit.com", "redd.it", "bilibili.com", "b23.tv", "nicovideo.jp", "nico.ms",
    "open.spotify.com", "spotify.com",
]
SPOTDL_MAX_TRACKS: int = _env_int("SPOTDL_MAX_TRACKS", 15)

# ============================================================
# Persistencia opcional en Upstash Redis (free tier, REST puro)
# ============================================================
# Si se definen ambas vars, la configuracion por usuario y las stats del bot
# sobreviven a reinicios/spin-downs/deployments. Sin ellas, todo queda en
# memoria como siempre (degradacion graceful).
_REDIS_URL: str = os.environ.get("UPSTASH_REDIS_REST_URL", "").strip().rstrip("/")
_REDIS_TOKEN: str = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "").strip()
_REDIS_ON: bool = bool(_REDIS_URL and _REDIS_TOKEN)


def _redis_cmd(*args):
    """Ejecuta un comando Redis via API REST de Upstash (bloqueante)."""
    resp = requests.post(
        _REDIS_URL,
        headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
        json=list(args),
        timeout=10,
    )
    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"upstash: {data['error']}")
    return data.get("result")


def _redis_bg(*args) -> None:
    """Comando Redis fire-and-forget en thread daemon (nunca bloquea)."""
    def _run():
        try:
            _redis_cmd(*args)
        except Exception as e:
            logger.debug(f"redis bg fallo: {e}")
    threading.Thread(target=_run, daemon=True).start()
# deno (runtime JS) es requerido por yt-dlp para extraer streams de YouTube
# sin 403. El Build Command de Render lo deja en ./bin/deno; en dev debe
# estar en el PATH manualmente.
_bin_dir: str = os.path.join(os.getcwd(), "bin")
if os.path.isdir(_bin_dir):
    os.environ["PATH"] = _bin_dir + os.pathsep + os.environ.get("PATH", "")


def _deno_available() -> bool:
    return bool(shutil.which("deno"))


# Cookies de YouTube para saltar el "Sign in to confirm you're not a bot"
# que YouTube impone a IPs de datacenter (Render). Se pasan en base64 via
# env var YOUTUBE_COOKIES_B64 (NUNCA commitear el cookies.txt al repo):
#   PowerShell: [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt"))
YT_COOKIES_B64: str = os.environ.get("YOUTUBE_COOKIES_B64", "").strip()
_yt_cookies_path: Optional[str] = None
if YT_COOKIES_B64:
    try:
        import base64
        _yt_cookies_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        with open(_yt_cookies_path, "wb") as _cfh:
            _cfh.write(base64.b64decode(YT_COOKIES_B64))
        logger.info("Cookies de YouTube decodificadas desde YOUTUBE_COOKIES_B64")
    except Exception as _ce:
        logger.warning(f"No se pudo decodificar YOUTUBE_COOKIES_B64: {_ce}")
        _yt_cookies_path = None


def _apply_yt_guard(opts: dict) -> dict:
    """Inyecta las defensas anti-bot de YouTube en opts de yt-dlp.

    deno (JS runtime) + cookies + proxy outbound, en ese orden. Usarlo en
    TODA extraccion/descarga que toque youtube.com.
    """
    if _yt_cookies_path:
        opts["cookiefile"] = _yt_cookies_path
    if _deno_available():
        opts["js_runtimes"] = {"deno": {}}
    if OUTBOUND_PROXY_URL:
        opts["proxy"] = OUTBOUND_PROXY_URL
    return opts
COOKIES_FILE: str = os.environ.get("COOKIES_FILE") or os.path.join(tempfile.gettempdir(), "cookies.txt")
CACHE_DIR: str = os.environ.get("YDL_CACHE_DIR") or os.path.join(tempfile.gettempdir(), "ydl_cache")
MAX_URLS_PER_MESSAGE: int = _env_int("MAX_URLS_PER_MESSAGE", 20)
# Proxy opcional para las salidas bloqueadas por reputacion de IP (TikTok,
# tikwm): http:// o socks5://. Si se define, lo usan tikwm, la descarga
# directa de media y yt-dlp (opts["proxy"]).
OUTBOUND_PROXY_URL: str = (os.environ.get("OUTBOUND_PROXY_URL") or "").strip()

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
MAX_FILE_BYTES: int = 50 * 1024 * 1024
# Escalera de rescate: el filtro filesize de yt-dlp falla ABIERTO cuando no
# conoce el tamano real del stream (DASH/HLS), por eso un merge puede terminar
# pesando >50MB. Ante eso se reintenta capando resolucion y, si persiste,
# con el peor formato disponible. El operador <? hace que la comparacion
# PASA cuando filesize es desconocido (en vez de excluir el formato).
_DOWNLOAD_RESCUE_FORMATS: tuple[str, ...] = (
    "b[vheight<=720][filesize<?48M]/bv*[vheight<=720][filesize<?42M]+ba/worst",
    "worst",
)

# ============================================================
# Estadisticas globales (thread-safe)
# ============================================================
# Cota para los dicts de seguimiento por-usuario: superado el limite se
# conserva la mitad mas reciente (evita crecimiento sin techo en despliegues
# longevos; en free tier los restarts ya lo limitan, esto es cinturon y
# tirantes).
_MAX_TRACKED_USERS: int = 10_000


def _trim_by_recency(d: dict, max_size: int) -> None:
    """Conserva solo la mitad mas reciente de un dict {id: timestamp}."""
    if len(d) <= max_size:
        return
    keep = sorted(d.items(), key=lambda kv: kv[1], reverse=True)[: max_size // 2]
    d.clear()
    d.update(keep)


_stats: dict = {
    "start_time": time.time(),
    "total_requests": 0,
    "successful": 0,
    "failed": 0,
    "unique_users": {},    # user_id -> primera vez visto; nunca serializado
    "by_platform": {},     # {"tiktok": {"successful": n, "failed": n}, ...}
}
_stats_lock: threading.Lock = threading.Lock()

def _inc_stats(key: str) -> None:
    """Incrementa una estadistica numerica de forma thread-safe."""
    with _stats_lock:
        _stats[key] += 1
    if key == "total_requests":
        _mark_dirty("total_requests")

def _add_unique_user(user_id: int) -> None:
    """Registra un usuario unico de forma thread-safe.

    Si el registro excede la cota, se recorta a la mitad mas reciente: un
    usuario antiguo podria volver a contar como nuevo, tolerable frente a
    dejar crecer el dict sin limite.
    """
    with _stats_lock:
        users = _stats["unique_users"]
        if user_id not in users:
            _trim_by_recency(users, _MAX_TRACKED_USERS)
            users[user_id] = time.time()
    _mark_dirty("users")


# ------------------------------------------------------------
# Volcado periodico de stats a Redis (si esta configurado)
# ------------------------------------------------------------
_stats_dirty_lock: threading.Lock = threading.Lock()
_stats_dirty: dict[str, int] = {}
_users_floor: int = 0  # contador persistido; puede superar al set recortado


def _mark_dirty(field: str, delta: int = 1) -> None:
    if not _REDIS_ON:
        return
    with _stats_dirty_lock:
        _stats_dirty[field] = _stats_dirty.get(field, 0) + delta


def _restore_and_start_flusher() -> None:
    """Restaura stats desde Redis y arranca el flusher periodico (thread daemon).

    Llamado desde ensure_bot() una vez listo el bot: NUNCA al importar
    (gunicorn --preload heredaria el thread roto tras el fork).
    """
    global _users_floor
    if not _REDIS_ON:
        return
    try:
        data = _redis_cmd("HGETALL", "bot_stats") or {}
        with _stats_lock:
            for field, val in data.items():
                try:
                    v = int(val)
                except (TypeError, ValueError):
                    continue
                if field.startswith("p:"):
                    parts = field.split(":", 2)
                    if len(parts) == 3:
                        slot = _stats["by_platform"].setdefault(
                            parts[1], {"successful": 0, "failed": 0}
                        )
                        key = "successful" if parts[2] == "ok" else "failed"
                        slot[key] = max(slot.get(key, 0), v)
                elif field == "users":
                    _users_floor = max(_users_floor, v)
                elif field in ("total_requests", "successful", "failed"):
                    _stats[field] = max(int(_stats.get(field, 0)), v)
        logger.info(f"Stats restauradas desde Upstash (total={_stats['total_requests']})")
    except Exception as e:
        logger.warning(f"Restauracion de stats fallo: {e}")

    def _flush_loop() -> None:
        while True:
            time.sleep(30)
            with _stats_dirty_lock:
                pending = dict(_stats_dirty)
                _stats_dirty.clear()
            if not pending:
                continue
            try:
                for field, delta in pending.items():
                    _redis_cmd("HINCRBY", "bot_stats", field, delta)
            except Exception as e:
                logger.debug(f"flush de stats fallo: {e}")

    threading.Thread(target=_flush_loop, daemon=True).start()
    logger.info("Flusher de stats hacia Upstash activo")


def _platform_of(url: str) -> str:
    """Devuelve el nombre de plataforma a partir de una URL (para stats)."""
    u: str = (url or "").lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "facebook.com" in u or "fb.com" in u:
        return "facebook"
    if "reddit.com" in u or "redd.it" in u:
        return "reddit"
    return "otro"


def _record_result(url: str, success: bool) -> None:
    """Registra una descarga exitosa/fallida, tanto global como por plataforma."""
    _inc_stats("successful" if success else "failed")
    platform: str = _platform_of(url)
    with _stats_lock:
        p: dict = _stats["by_platform"].setdefault(platform, {"successful": 0, "failed": 0})
        p["successful" if success else "failed"] += 1
    if _REDIS_ON:
        kind = "ok" if success else "fail"
        _mark_dirty(f"p:{platform}:{kind}")

# ============================================================
# Rate limiting por usuario
# ============================================================
_user_last_request: dict[int, float] = {}
_user_cooldown_lock: threading.Lock = threading.Lock()

def _check_cooldown(user_id: int) -> float:
    """Retorna los segundos restantes de cooldown, o 0 si puede proceder."""
    with _user_cooldown_lock:
        now = time.time()
        _trim_by_recency(_user_last_request, _MAX_TRACKED_USERS)
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
        _trim_by_recency(_chat_action_last_sent, _MAX_TRACKED_USERS)
        last = _chat_action_last_sent.get(chat_id, 0)
        if now - last < CHAT_ACTION_INTERVAL:
            return
        _chat_action_last_sent[chat_id] = now
    try:
        await bot.send_chat_action(chat_id=chat_id, action=action)
    except (TelegramTimedOut, TelegramNetworkError, TelegramRetryAfter):
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
    meta: Optional[dict] = None  # payload extra (ej. track de Spotify)

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
    """Mensaje de bienvenida con plataformas, funciones de anime y sistema de cola."""
    user = update.effective_user
    logger.info(f"Comando /start de {user.id} (@{user.username})")
    text = (
        "\U0001f44b \u00a1Hola! Soy tu bot de descargas.\n\n"
        "\U0001f4e5 <b>Descarga videos de:</b>\n"
        "\u2022 TikTok (sin marca de agua)\n"
        "\u2022 Facebook (videos / Reels)\n"
        "\u2022 Twitter / X (videos, GIFs e imagenes)\n"
        "\u2022 Reddit (videos, imagenes y GIFs)\n"
        "\u2022 Bilibili (anime, clips, AMVs)\n"
        "\u2022 Niconico (anime, MADs, musica)\n\n"
        "\U0001f3b5 <b>Musica:</b> envia un link de Spotify (track, album o playlist) y elige que descargar.\n\n"
        "\U0001f38c <b>Funciones de anime:</b>\n"
        "\u2022 /anime &lt;nombre&gt; \u2014 info + sinopsis en espanol\n"
        "\u2022 /manga &lt;nombre&gt; \u2014 info de manga\n"
        "\u2022 /temporada \u2014 animes de la temporada\n"
        "\u2022 /hoy \u2014 emisiones de las proximas 24h\n"
        "\u2022 /waifu \u2014 imagen random\n"
        "\u2022 Enviame un screenshot y lo identifico (anime, episodio y tiempo)\n\n"
        "\U0001f4e6 <b>Cola por usuario:</b>\n"
        "Puedes enviar varios enlaces seguidos. Se procesaran en orden.\n"
        "Usa /queue para ver tus pendientes y /cancel para vaciar la cola.\n\n"
        "\u2699\ufe0f Usa /config para elegir si ver el credito del bot y el titulo del video.\n\n"
        "\u26a0\ufe0f Limite: 50 MB por archivo."
    )
    await update.message.reply_text(text, parse_mode="HTML")


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
            f"\U0001f4ca <b>Estadisticas del Bot</b>\n\n"
            f"\U0001f550 <b>Activo:</b> {uptime_str}\n"
            f"\U0001f4e5 <b>Solicitudes totales:</b> {_stats['total_requests']}\n"
            f"\u2705 <b>Exitosas:</b> {_stats['successful']}\n"
            f"\u274c <b>Fallidas:</b> {_stats['failed']}\n"
            f"\U0001f465 <b>Usuarios unicos:</b> {max(len(_stats['unique_users']), _users_floor)}\n"
            f"\U0001f4e6 <b>Colas activas:</b> {len(_user_queues)}\n"
        )
        platform_lines: list[str] = []
        for name in ("tiktok", "twitter", "facebook", "reddit", "otro"):
            p = _stats["by_platform"].get(name)
            if p and (p.get("successful") or p.get("failed")):
                platform_lines.append(
                    f"\u2022 {name}: {p.get('successful', 0)}\u2705 / {p.get('failed', 0)}\u274c"
                )
        if platform_lines:
            text += "\n\U0001f4ca <b>Por plataforma:</b>\n" + "\n".join(platform_lines)

    await update.message.reply_text(text, parse_mode="HTML")


async def cancel(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Vacia la cola de pendientes del usuario.

    No interrumpe una descarga ya en curso: solo elimina las tareas encoladas.
    El worker seguira vivo, terminara lo que este haciendo y luego se
    autolimpiara tras el periodo de inactividad habitual.
    """
    user = update.effective_user
    queue = _user_queues.get(user.id)
    if not queue:
        await update.message.reply_text("\u2139\ufe0f No tienes tareas en la cola.")
        return

    drained: int = 0
    while True:
        try:
            queue.get_nowait()
            queue.task_done()
            drained += 1
        except asyncio.QueueEmpty:
            break

    logger.info(f"Cola cancelada para {user.id}: {drained} tarea(s) eliminada(s)")

    if drained == 0:
        await update.message.reply_text("\u2139\ufe0f No tienes tareas pendientes en la cola.")
    else:
        await update.message.reply_text(
            f"\u2705 Canceladas <b>{drained}</b> tarea(s) pendientes.\n"
            "Si hay una descarga en curso, se completara igualmente.",
            parse_mode="HTML",
        )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias de /start."""
    await start(update, context)


async def show_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra el chat_id y user_id (util para configurar ADMIN_IDS)."""
    user = update.effective_user
    chat = update.effective_chat
    await update.message.reply_text(
        f"\U0001f194 <b>Tus IDs</b>\n\n"
        f"\U0001f464 <b>User ID:</b> <code>{user.id}</code>\n"
        f"\U0001f4ac <b>Chat ID:</b> <code>{chat.id}</code>",
        parse_mode="HTML",
    )


async def queue_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra las descargas pendientes del usuario en su cola."""
    user = update.effective_user
    queue = _user_queues.get(user.id)
    # Lectura directa de la deque interna: el event loop es unico y no hay
    # modificacion concurrente durante la ejecucion de este handler.
    tasks = list(queue._queue) if queue else []
    if not tasks:
        await update.message.reply_text("\U0001f4ed No tienes descargas pendientes en la cola.")
        return
    lines: list[str] = []
    for i, t in enumerate(tasks, 1):
        url_short: str = t.url if len(t.url) <= 60 else t.url[:57] + "..."
        lines.append(f"{i}. {url_short}")
    text: str = (
        f"\U0001f4e6 **Cola de descargas** ({len(tasks)} pendiente(s)):\n\n"
        + "\n".join(lines)
    )
    await update.message.reply_text(text, disable_web_page_preview=True)

# ============================================================
# Configuracion por usuario (captions configurables via /config)
# ============================================================
_user_settings: dict[int, dict] = {}


def _get_user_settings(user_id: int) -> dict:
    """Retorna las preferencias del usuario, creandolas con defaults si no existen."""
    return _user_settings.setdefault(
        user_id, {"show_credit": True, "show_title": True, "fmt": "mp3"}
    )


_SPOT_FMTS: tuple[str, ...] = ("mp3", "m4a", "opus", "flac")


def _build_caption(task: DownloadTask, title: str = "") -> str:
    """Construye el caption de una descarga segun las preferencias del usuario."""
    s = _get_user_settings(task.user_id)
    parts: list[str] = []
    t: str = (title or "").strip()
    if s.get("show_title", True) and t:
        parts.append(t if len(t) <= 200 else t[:197] + "...")
    if s.get("show_credit", True):
        parts.append(f"\U0001f4e5 Descargado por @{task.bot_username}")
    return "\n".join(parts)


async def _load_remote_settings(user_id: int) -> None:
    """Carga desde Redis las preferencias del usuario si no estan en memoria."""
    if not _REDIS_ON or user_id in _user_settings:
        return
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    def _load():
        raw = _redis_cmd("GET", f"spotcfg:{user_id}")
        if isinstance(raw, str):
            _user_settings[user_id] = {**_get_user_settings(user_id), **json.loads(raw)}

    try:
        await loop.run_in_executor(_download_executor, _load)
    except Exception as e:
        logger.debug(f"carga remota de settings fallo: {e}")


def _save_user_settings_remote(user_id: int, s: dict) -> None:
    """Persiste las preferencias en Redis (fire-and-forget) si esta activo."""
    if _REDIS_ON:
        _redis_bg("SET", f"spotcfg:{user_id}", json.dumps(s))


def _build_config_keyboard(s: dict) -> InlineKeyboardMarkup:
    on: str = "\u2705 ON"
    off: str = "\u274c OFF"
    credit: str = on if s.get("show_credit", True) else off
    title: str = on if s.get("show_title", True) else off
    fmt: str = str(s.get("fmt", "mp3")).upper()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Credito del bot: {credit}", callback_data="cfg:credit")],
        [InlineKeyboardButton(f"Titulo/descripcion: {title}", callback_data="cfg:title")],
        [InlineKeyboardButton(f"Formato musica: {fmt}", callback_data="cfg:fmt")],
    ])


async def config_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra y permite editar las preferencias personales del usuario.

    Con Upstash configurado, los ajustes persisten entre reinicios.
    """
    uid = update.effective_user.id
    await _load_remote_settings(uid)
    s = _get_user_settings(uid)
    await update.message.reply_text(
        "\u2699\ufe0f <b>Configuracion</b>\n\n"
        "\u2022 <b>Credito</b>: muestra \"Descargado por @bot\" en cada archivo.\n"
        "\u2022 <b>Titulo</b>: muestra la descripcion/titulo del video.\n"
        "\u2022 <b>Formato musica</b>: para los links de Spotify "
        "(la calidad siempre es la maxima real que entrega la fuente).\n\n"
        "Toca un boton para ciclar.",
        parse_mode="HTML",
        reply_markup=_build_config_keyboard(s),
    )


async def config_cb(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback de los botones de /config: alterna la preferencia y redibuja."""
    q = update.callback_query
    if q is None or not q.data:
        return
    await q.answer()
    s = _get_user_settings(q.from_user.id)
    if q.data == "cfg:credit":
        s["show_credit"] = not s.get("show_credit", True)
    elif q.data == "cfg:title":
        s["show_title"] = not s.get("show_title", True)
    elif q.data == "cfg:fmt":
        cur = str(s.get("fmt", "mp3"))
        s["fmt"] = _SPOT_FMTS[(_SPOT_FMTS.index(cur) + 1) % len(_SPOT_FMTS)] if cur in _SPOT_FMTS else _SPOT_FMTS[0]
    else:
        return
    _save_user_settings_remote(q.from_user.id, s)
    try:
        await q.edit_message_reply_markup(reply_markup=_build_config_keyboard(s))
    except TelegramBadRequest:
        pass

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
        # Escalera anti->50MB: si los streams tienen filesize conocido y
        # exceden 50M (ej. Reddit 1080p), los primeros filtros los saltan
        # correctamente y caeriamos al merge SIN techo bv*+ba. El escalon
        # bv*[height<=720] entrega antes una variante que SI cabe.
        "format": (
            "best[filesize<50M]"
            "/bv*[height<=720][filesize<50M]+ba"
            "/b[height<=720]"
            "/bv*+ba/best"
        ),
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
    if OUTBOUND_PROXY_URL:
        opts["proxy"] = OUTBOUND_PROXY_URL
        logger.info(f"yt-dlp usando proxy outbound para saltar bloqueos de IP")
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
    short_hosts = ("vt.tiktok.com", "vm.tiktok.com")
    if parsed.hostname and parsed.hostname.replace("www.", "") in short_hosts:
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


def _resolve_short_url(url: str) -> str:
    """Resuelve acortadores conocidos (b23.tv, nico.ms) a su URL final.

    Retorna la URL resuelta (sin query params) o la original si falla.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").replace("www.", "")
    if host not in ("b23.tv", "nico.ms"):
        return url
    try:
        head = requests.head(
            url, allow_redirects=True, timeout=HTTP_SHORT_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        resolved: str = head.url.split("?")[0]
        logger.info(f"URL corta {host} resuelta: {resolved}")
        return resolved
    except Exception as e:
        logger.warning(f"No se pudo resolver URL corta {host}: {e}")
        return url


_TIKWM_PROFILES: tuple[str, ...] = ("chrome", "firefox133", "chrome124")


def _tikwm_post(api_url: str) -> Optional[CurlResponse]:
    """POST form-urlencoded a tikwm.com impersonando navegadores reales.

    tikwm usa Cloudflare: bloquea fingerprints TLS no-navegador y, desde IPs
    con mala reputacion (datacenters como Render), a veces tambien perfiles
    concretos. Se rota entre varios perfiles anadiendo cabeceras tipicas de
    una visita web; si OUTBOUND_PROXY_URL esta definida, sale por ahi.
    """
    extra_headers: dict = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://tikwm.com/",
        "Origin": "https://tikwm.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    last_detail: str = "sin respuesta"
    for attempt, profile in enumerate(_TIKWM_PROFILES, 1):
        try:
            resp: CurlResponse = curl_requests.post(
                "https://tikwm.com/api/",
                data={"url": api_url},
                timeout=HTTP_MEDIUM_TIMEOUT,
                impersonate=profile,
                headers=extra_headers,
                **({"proxy": OUTBOUND_PROXY_URL} if OUTBOUND_PROXY_URL else {}),
            )
            ct: str = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "json" in ct.lower():
                return resp
            last_detail = (
                f"HTTP {resp.status_code} ({ct or 'sin content-type'}) "
                f"[{profile}]: {resp.text[:150]}"
            )
        except Exception as e:
            last_detail = f"{type(e).__name__}: {e} [{profile}]"
        logger.warning(
            f"tikwm.com intento {attempt}/{len(_TIKWM_PROFILES)} fallo: {last_detail}"
        )
        if attempt < len(_TIKWM_PROFILES):
            # El free tier de tikwm limita a 1 request/segundo: esperar evita
            # quemar los siguientes perfiles con un rate-limit artificial.
            time.sleep(1.1)
    logger.warning(f"tikwm.com agoto los perfiles de navegador: {last_detail}")
    return None


def _tiktok_api_fallback(url: str) -> Optional[tuple]:
    """
    Fallback para TikTok slideshows usando la API de tikwm.com
    cuando yt-dlp no detecta el slideshow correctamente.
    """
    logger.info(f"Usando fallback tikwm.com para {url}")
    api_url: str = _resolve_tiktok_url(url)
    resp = _tikwm_post(api_url)
    if resp is None:
        return None
    try:
        data: dict = resp.json()
    except ValueError:
        logger.warning("tikwm.com devolvio JSON invalido pese al content-type")
        return None
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
    try:
        api_url: str = _resolve_tiktok_url(url)

        resp = _tikwm_post(api_url)
        if resp is None:
            return None
        try:
            data: dict = resp.json()
        except ValueError:
            logger.warning("tikwm.com devolvio JSON invalido pese al content-type")
            return None
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

        r = curl_requests.get(
            video_url,
            timeout=HTTP_DOWNLOAD_TIMEOUT,
            impersonate="chrome",
            **({"proxy": OUTBOUND_PROXY_URL} if OUTBOUND_PROXY_URL else {}),
        )
        r.raise_for_status()
        filename: str = os.path.join(
            tempfile.gettempdir(),
            f"tiktok_video_{uuid.uuid4().hex}.mp4",
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
# Extraccion de URLs de mensajes
# ============================================================
_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")


def _extract_urls(text: str) -> list[str]:
    """Extrae URLs de un mensaje (sueltas o embebidas en texto), sin duplicados."""
    seen: set[str] = set()
    urls: list[str] = []
    for u in _URL_PATTERN.findall(text):
        u = u.rstrip(".,;:!?)]}>")
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


# ============================================================
# Fallback para imagenes de tweets (Twitter/X)
# ============================================================

def _twitter_media_fallback(url: str) -> Optional[tuple[list[str], list[str]]]:
    """
    Fallback para tweets que contienen imagenes y/o GIFs en lugar de video.
    Usa la API publica de fxtwitter/vxtwitter para clasificar el contenido.

    Retorna (urls_de_imagenes, urls_de_gifs) o None si falla. Los GIFs de X
    son MP4 (tweet_video/*.mp4) y DEBEN enviarse via sendAnimation: mandarlos
    como foto los convierte en una imagen estatica.
    """
    match = re.search(r"/status/(\d+)", url)
    if not match:
        logger.warning("Twitter fallback: no se pudo extraer el id del tweet")
        return None
    tweet_id: str = match.group(1)
    headers: dict = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    for api in (
        f"https://api.fxtwitter.com/i/status/{tweet_id}",
        f"https://api.vxtwitter.com/i/status/{tweet_id}",
    ):
        try:
            resp = requests.get(api, headers=headers, timeout=HTTP_MEDIUM_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"Twitter fallback: {api} respondio {resp.status_code}")
                continue
            data: dict = resp.json()
            imgs: list[str] = []
            gifs: list[str] = []
            for m in (data.get("media_extended") or []):
                if not isinstance(m, dict) or not m.get("url"):
                    continue
                t: str = str(m.get("type", "")).lower()
                if t == "gif":
                    gifs.append(m["url"])
                elif t == "image":
                    imgs.append(m["url"])
            if not imgs and not gifs:
                # Fallback por extension cuando la API no trae media_extended
                for u in (data.get("mediaURLs") or []):
                    if not isinstance(u, str) or not u:
                        continue
                    ext: str = os.path.splitext(urlparse(u).path)[1].lower()
                    if ext == ".mp4" or "/tweet_video/" in u:
                        gifs.append(u)
                    else:
                        imgs.append(u)
            if imgs or gifs:
                logger.info(
                    f"Twitter fallback: {len(imgs)} imagenes / {len(gifs)} gifs via {api}"
                )
                return imgs, gifs
        except Exception as e:
            logger.warning(f"Twitter fallback {api} fallo: {e}")
    return None


def _download_images(urls: list[str], prefix: str, default_ext: str = ".jpg") -> list[str]:
    """Descarga una lista de imagenes/videos cortos a tempdir.

    default_ext se usa cuando la URL no tiene extension conocida (.jpg para
    fotos de tweets, .mp4 para GIFs de X).
    """
    paths: list[str] = []
    headers: dict = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://x.com/",
    }
    for i, u in enumerate(urls):
        if not u:
            continue
        ext: str = os.path.splitext(urlparse(u).path)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4"):
            ext = default_ext
        path: str = os.path.join(tempfile.gettempdir(), f"{prefix}_{uuid.uuid4().hex}_{i}{ext}")
        ok = False
        for attempt in range(1, 4):
            try:
                r = requests.get(u, headers=headers, timeout=HTTP_LONG_TIMEOUT)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                paths.append(path)
                ok = True
                break
            except Exception as e:
                logger.warning(f"Imagen {i} intento {attempt}/3 fallo: {e}")
                if attempt < 3:
                    time.sleep(1)
        if not ok:
            logger.warning(f"Imagen {i} no se pudo descargar: {u}")
    return paths


# ============================================================
# Musica via spotdl (metadata) + yt-dlp/ffmpeg (audio)
# ============================================================
# spotdl SOLO provee los metadatos de Spotify (`spotdl save`, usa credenciales
# default incluidas). La descarga la hace nuestro propio pipeline yt-dlp +
# ffmpeg: el proveedor interno de spotdl es fragil ante el anti-bot actual de
# YouTube, mientras que yt-dlp reciente + deno en PATH funciona.

_spotdl_jobs: dict[str, dict] = {}  # job_id -> {owner, ts, name, tracks}


def _spotdl_save(url: str, out_dir: str) -> list[dict]:
    """Corre `spotdl save` y retorna la lista cruda de tracks del JSON."""
    save_file: str = os.path.join(out_dir, "list.spotdl")
    cmd = [sys.executable, "-m", "spotdl", "save", url, "--save-file", save_file]
    proc = subprocess.run(cmd, capture_output=True, timeout=120)
    if proc.returncode != 0 or not os.path.isfile(save_file):
        tail = (proc.stderr or b"")[-200:].decode("utf-8", "ignore")
        raise RuntimeError(f"spotdl save fallo: {tail or 'sin salida'}")
    with open(save_file, encoding="utf-8") as fh:
        data = json.load(fh)
    tracks = data.get("tracks") if isinstance(data, dict) else data
    return tracks or []


def _spotify_parse_tracks(raw: list[dict], max_tracks: int) -> tuple[str, list[dict], int]:
    """Convierte tracks crudos de spotdl a (nombre_lista, tracks, omitidas)."""
    list_name: str = ""
    out: list[dict] = []
    for t in raw:
        name = t.get("name") or "?"
        artists_raw = t.get("artists") or ([t["artist"]] if t.get("artist") else [])
        artists_s = ", ".join(
            a if isinstance(a, str) else (a.get("name") or "?") for a in artists_raw
        )
        entry = {
            "name": name,
            "artists": artists_s or "Desconocido",
            "dur": int(t.get("duration") or 0),
            "query": f"{artists_s} - {name}",
            "cover": t.get("cover_url") or "",
        }
        if len(out) < max_tracks:
            out.append(entry)
        if not list_name and t.get("list_name"):
            list_name = str(t["list_name"])
    omitted = max(0, len(raw) - max_tracks)
    return list_name, out, omitted


def _spotdl_search_match(query: str, expected_dur: int) -> Optional[str]:
    """Busca en YouTube Music y retorna la URL del mejor match por duracion.

    Score = diferencia absoluta de duracion; tolerancia 20s antes de caer al
    primer resultado.
    """
    search_opts: dict = {"quiet": True, "no_warnings": True, "noplaylist": True,
                         "socket_timeout": 30}
    _apply_yt_guard(search_opts)
    with yt_dlp.YoutubeDL(search_opts) as ydl:
        sinfo = ydl.extract_info(f"ytsearch5:{query}", download=False)
    entries = [e for e in (sinfo.get("entries") or []) if e]
    if not entries:
        return None
    best = min(entries, key=lambda e: abs(int(e.get("duration") or 0) - expected_dur))
    if expected_dur and abs(int(best.get("duration") or 0) - expected_dur) > 20:
        best = entries[0]
    vid = best.get("id")
    return f"https://www.youtube.com/watch?v={vid}" if vid else None


def _spotdl_fetch_mp3(query: str, expected_dur: int, out_dir: str,
                      fmt: str = "mp3", quality: str = "0") -> Optional[str]:
    """Descarga el audio del mejor match y lo convierte al formato dado.

    Calidad "0" = VBR maximo en ffmpeg, que se adapta al techo REAL de la
    fuente (YouTube entrega ~128-160k): fijar 320k solo inflaria el archivo.
    Formatos soportados por FFmpegExtractAudio: mp3/m4a/opus/flac (flac es
    lossless e ignora la calidad).
    """
    video_url = _spotdl_search_match(query, expected_dur)
    if not video_url:
        logger.warning(f"spotdl: sin resultados para '{query[:60]}'")
        return None
    token = uuid.uuid4().hex[:10]
    opts: dict = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, f"spt_{token}.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "socket_timeout": 60,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
            "preferredquality": quality,
        }],
    }
    _apply_yt_guard(opts)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(video_url, download=True)
    outs = sorted(glob_module(os.path.join(out_dir, f"spt_{token}*.{fmt}")),
                  key=os.path.getsize, reverse=True)
    return outs[0] if outs else None


def glob_module(pattern: str) -> list[str]:
    """Wrapper de glob.glob para tests/monkeypatching sin import global."""
    import glob as _glob
    return _glob.glob(pattern)


async def _start_spot_listing(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra la info de un link de Spotify y botones para descargar por pista."""
    user = update.effective_user
    wait_msg: Message = await update.message.reply_text("\U0001f50d Obteniendo informacion de Spotify...")
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    def _job() -> tuple[str, list[dict], int]:
        d = tempfile.mkdtemp(prefix="spj_")
        try:
            raw = _spotdl_save(url, d)
            return _spotify_parse_tracks(raw, SPOTDL_MAX_TRACKS)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    try:
        lname, tracks, omitted = await loop.run_in_executor(_download_executor, _job)
    except Exception as e:
        logger.warning(f"Spotify listing fallo: {e}")
        await wait_msg.edit_text(
            "\u274c No pude leer ese link de Spotify.\n"
            "Verifica que sea un track, album o playlist publica."
        )
        return

    if not tracks:
        await wait_msg.edit_text("\u274c Ese link no contiene pistas descargables.")
        return

    job_id = uuid.uuid4().hex[:12]
    _spotdl_jobs[job_id] = {"owner": user.id, "ts": time.time(),
                            "name": lname or "Spotify", "tracks": tracks}
    _trim_by_recency(_spotdl_jobs, 20)

    header = (
        f"\U0001f3b5 <b>{html.escape(lname or 'Spotify')}</b> \u2014 {len(tracks)} pista(s)"
        + (f" <i>(+{omitted} omitidas, tope {SPOTDL_MAX_TRACKS})</i>" if omitted else "")
    )
    lines = [
        f"{i}. {html.escape(t['artists'])} \u2014 {html.escape(t['name'])} "
        f"<i>({t['dur'] // 60}:{t['dur'] % 60:02d})</i>"
        for i, t in enumerate(tracks, 1)
    ]
    text = header + "\n\n" + "\n".join(lines) + "\n\n\u2b07\ufe0f Toca un numero para descargarla."

    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for i in range(len(tracks)):
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"spt:{job_id}:{i}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if len(tracks) > 1:
        buttons.append([InlineKeyboardButton(
            f"\u2b07\ufe0f Todas ({len(tracks)})", callback_data=f"spt:{job_id}:all"
        )])

    await wait_msg.edit_text(
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def spot_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback de los botones de Spotify: encola la(s) pista(s) elegida(s)."""
    q = update.callback_query
    parts = (q.data or "").split(":")
    await q.answer()
    if len(parts) != 3:
        return
    _, job_id, sel = parts
    job = _spotdl_jobs.get(job_id)
    if not job:
        await q.answer("Lista expirada. Envía el link de nuevo.", show_alert=True)
        return
    if q.from_user.id != job["owner"]:
        await q.answer("Solo quien pidió la lista puede descargar.", show_alert=True)
        return

    if sel == "all":
        idxs: list[int] = list(range(len(job["tracks"])))
    else:
        try:
            idxs = [int(sel)]
        except ValueError:
            return

    chat_id = q.message.chat.id
    queue = _user_queues.setdefault(job["owner"], asyncio.Queue())
    enqueued = 0
    for i in idxs:
        if i >= len(job["tracks"]):
            continue
        t = job["tracks"][i]
        task = DownloadTask(
            url=f"https://open.spotify.com/track/{t['query']}",
            chat_id=chat_id,
            user_id=job["owner"],
            message_id=q.message.message_id,
            bot_username=context.bot.username,
            processing_msg_id=None,
            meta={"kind": "spotdl", **t},
        )
        await queue.put(task)
        enqueued += 1

    worker = _queue_workers.get(job["owner"])
    if not worker or worker.done():
        _queue_workers[job["owner"]] = asyncio.create_task(_queue_worker(job["owner"]))
    await q.answer(f"\U0001f3b5 {enqueued} pista(s) en cola" if enqueued > 1 else "\U0001f3b5 En cola")


async def _execute_spotdl_task(task: DownloadTask) -> None:
    """Descarga una pista de Spotify via match YT Music + MP3, y la envia."""
    bot = application.bot
    meta = task.meta or {}
    query: str = meta.get("query", "")
    name: str = meta.get("name", "")
    artists: str = meta.get("artists", "")
    dur: int = int(meta.get("dur") or 0)

    out_dir = tempfile.mkdtemp(prefix="spt_")
    thumb_path: Optional[str] = None
    try:
        try:
            await bot.edit_message_text(
                chat_id=task.chat_id, message_id=task.processing_msg_id,
                text=f"\U0001f3a5 Buscando: {artists} - {name}",
            )
        except Exception:
            pass
        if task.processing_msg_id is None:
            try:
                msg: Message = await bot.send_message(task.chat_id, "\U0001f3a5 Buscando...")
                task.processing_msg_id = msg.message_id
            except Exception:
                pass

        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        await _load_remote_settings(task.user_id)
        s = _get_user_settings(task.user_id)
        fmt = str(s.get("fmt", "mp3"))
        mp3: Optional[str] = await loop.run_in_executor(
            _download_executor,
            lambda: _spotdl_fetch_mp3(query, dur, out_dir, fmt=fmt),
        )
        if not mp3:
            raise RuntimeError("sin resultados en YouTube Music")

        # Portada como thumbnail (JPEG <=200KB); best-effort
        cover_url: str = meta.get("cover") or ""
        if cover_url:
            try:
                r = requests.get(cover_url, timeout=15,
                                 headers={"User-Agent": "Mozilla/5.0"})
                if r.ok and len(r.content) <= 200 * 1024 and r.content[:3] == b"\xff\xd8\xff":
                    thumb_path = os.path.join(tempfile.gettempdir(), f"cov_{uuid.uuid4().hex}.jpg")
                    with open(thumb_path, "wb") as fh:
                        fh.write(r.content)
            except Exception:
                pass

        caption = _build_caption(task, f"{artists} - {name}")
        thumb_fh = open(thumb_path, "rb") if thumb_path else None
        try:
            await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_DOCUMENT)
            await _send_file_with_retry(
                bot, mp3,
                lambda f: bot.send_audio(
                    chat_id=task.chat_id,
                    audio=f,
                    caption=caption,
                    title=name,
                    performer=artists,
                    thumbnail=thumb_fh,
                    reply_to_message_id=task.message_id,
                    read_timeout=TG_READ_TIMEOUT,
                    write_timeout=TG_WRITE_TIMEOUT,
                    connect_timeout=TG_CONNECT_TIMEOUT,
                ),
            )
        finally:
            if thumb_fh:
                thumb_fh.close()

        _record_result(task.url, True)
        logger.info(f"Pista enviada: {artists} - {name}")
    except Exception as e:
        logger.warning(f"spotdl tarea fallo ({query[:50]}): {e}")
        try:
            await bot.edit_message_text(
                chat_id=task.chat_id, message_id=task.processing_msg_id,
                text="\u274c No pude descargar esa cancion. Probá de nuevo.",
            )
        except Exception:
            pass
        _record_result(task.url, False)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                pass


# ============================================================
# Handler principal: recibe URLs y las encola
# ============================================================

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Extrae y valida la(s) URL(s) enviada(s) por el usuario y las encola.
    Soporta multiples URLs, sueltas o embebidas en texto.
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
            f"\u23f3 Espera <b>{remaining:.0f}s</b> antes de enviar mas enlaces.",
            parse_mode="HTML",
        )
        return

    candidate_urls: list[str] = _extract_urls(raw_text)
    logger.info(f"{len(candidate_urls)} URL(s) recibida(s) de {user.id}")

    if len(candidate_urls) > MAX_URLS_PER_MESSAGE:
        logger.warning(f"Exceso de URLs de {user.id}: {len(candidate_urls)} (max {MAX_URLS_PER_MESSAGE})")
        await update.message.reply_text(
            f"\u274c Maximo <b>{MAX_URLS_PER_MESSAGE} enlaces</b> por mensaje.\n"
            f"Enviaste {len(candidate_urls)}. Dividi en varios mensajes.",
            parse_mode="HTML",
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

    # Links de Spotify: flujo interactivo (listado + botones), no cola directa
    if len(valid_urls) == 1 and "spotify.com" in valid_urls[0].lower():
        await _start_spot_listing(valid_urls[0], update, context)
        return

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
            f"\u23f3 <b>{len(valid_urls)} enlaces encolados.</b>\n"
            "Se procesaran uno por uno en orden.",
            parse_mode="HTML",
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
                "login required": (
                    "\u274c Esta plataforma requiere iniciar sesion para descargar este contenido."
                ),
                "has been removed": (
                    "\u274c Este video fue eliminado de la plataforma."
                ),
                "video unavailable": (
                    "\u274c El video no esta disponible o fue eliminado."
                ),
                "not available in your country": (
                    "\u274c Este contenido tiene restriccion geografica y no se puede descargar."
                ),
                "private": (
                    "\u274c Este contenido es privado y no se puede descargar."
                ),
                "premium": (
                    "\u274c Este contenido requiere cuenta premium en la plataforma."
                ),
                "may not be comfortable for some audiences": (
                    "\u274c Este video fue marcado como <b>sensible</b> por TikTok.\n"
                    "No es posible descargarlo sin iniciar sesion."
                ),
                "Unexpected response from webpage request": (
                    "\u274c TikTok cambio algo en su sitio y el bot no puede descargar este video por ahora.\n"
                    "Ya se reporto el problema. Proba de nuevo mas tarde."
                ),
            }
            display_msg: str = (
                f"\u274c Error de descarga:\n<code>{html.escape(err_msg[:200])}</code>"
            )
            err_lower: str = err_msg.lower()
            for key, msg in friendly.items():
                if key.lower() in err_lower:
                    display_msg = msg
                    break

            # "Unsupported URL" depende de la plataforma: el extractor no
            # reconoce el tipo de enlace (ej. posts /photo/ con yt-dlp viejo).
            if "unsupported url" in err_lower:
                if "reddit.com" in url_lower or "redd.it" in url_lower:
                    display_msg: str = (
                        "\u274c Ese enlace de Reddit no contiene un video.\n"
                        "Solo puedo descargar posts de Reddit que tengan videos (v.redd.it) "
                        "o imagenes/GIFs individuales."
                    )
                elif "tiktok.com" in url_lower:
                    display_msg = (
                        "\u274c TikTok no reconoce ese enlace como contenido descargable.\n"
                        "Puede ser un post de fotos que la version actual del bot no soporta. "
                        "Proba de nuevo mas tarde."
                    )
                else:
                    display_msg = (
                        "\u274c Ese tipo de enlace no esta soportado por el extractor.\n"
                        "Verifica que sea un link directo a un video o imagen."
                    )

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
                    parse_mode="HTML",
                )
            except Exception:
                pass
            _record_result(task.url, False)

        except Exception as e:
            msg: str = str(e)[:200] or "(sin mensaje)"
            logger.error(f"Error inesperado para {task.url}: {msg}")
            logger.debug(traceback.format_exc())

            display_msg = (
                f"\u274c Error inesperado:\n<code>{html.escape(msg)}</code>"
            )

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
                    parse_mode="HTML",
                )
            except Exception:
                pass
            _record_result(task.url, False)

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
        except TelegramRetryAfter as e:
            last_exc = e
            delay: float = float(e.retry_after)
            logger.warning(
                f"Flood control (429) enviando {filename}: esperando {delay}s "
                f"(intento {attempt+1}/{max_retries})"
            )
            await asyncio.sleep(delay)
        except (TelegramTimedOut, TelegramNetworkError) as e:
            last_exc = e
            delay: float = WORKER_RETRY_DELAY_BASE * (2 ** attempt)
            logger.warning(f"Reintento {attempt+1}/{max_retries} en {delay}s: {e}")
            await asyncio.sleep(delay)
        except Exception:
            raise
    logger.error(f"Se agotaron los reintentos para enviar {filename}: {last_exc}")
    raise last_exc


def _download_url_photo(url: str) -> Optional[str]:
    """Descarga una imagen por URL a tempfile y retorna la ruta local."""
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            timeout=HTTP_DOWNLOAD_TIMEOUT,
        )
        r.raise_for_status()
        path_ext: str = os.path.splitext(urlparse(url).path)[1].lower()
        if path_ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            path_ext = ".jpg"
        path: str = os.path.join(
            tempfile.gettempdir(), f"url_photo_{uuid.uuid4().hex}{path_ext}"
        )
        with open(path, "wb") as fh:
            fh.write(r.content)
        return path
    except Exception as e:
        logger.warning(f"No se pudo descargar la imagen {url}: {e}")
        return None


async def _send_photo_url_safe(bot, chat_id: int, url: str, caption=None, reply_to=None):
    """sendPhoto por URL con fallback a upload directo.

    El Bot API solo acepta fotos de hasta 5 MB por URL (10 MB por upload);
    si Telegram rechaza la URL (BadRequest) se descarga localmente y se
    reenvia como archivo con los reintentos habituales.
    """
    try:
        return await bot.send_photo(
            chat_id=chat_id, photo=url, caption=caption,
            reply_to_message_id=reply_to,
        )
    except TelegramBadRequest as e:
        logger.warning(f"send_photo por URL fallo ({e}); probando upload directo")

    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    path: Optional[str] = await loop.run_in_executor(
        _download_executor, _download_url_photo, url
    )
    if not path:
        raise RuntimeError(f"No se pudo descargar la imagen para reenvio: {url}")
    try:
        return await _send_file_with_retry(
            bot, path,
            lambda f: bot.send_photo(
                chat_id=chat_id, photo=f, caption=caption,
                reply_to_message_id=reply_to,
                read_timeout=TG_READ_TIMEOUT, write_timeout=TG_WRITE_TIMEOUT,
                connect_timeout=TG_CONNECT_TIMEOUT,
            ),
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def _send_media_group_with_retry(
    bot,
    chat_id: int,
    paths: list[str],
    reply_to_message_id: int,
    caption_text: str,
    first_batch: bool,
    max_retries: int = 3,
):
    """
    Envia un album de fotos con reintentos (timeouts y flood control 429).

    Cada reintento re-abre los archivos para evitar punteros de lectura gastados
    y cierra SIEMPRE los descriptores en un bloque finally.
    """
    last_exc = None
    for attempt in range(max_retries):
        files: list = []
        try:
            media_group: list[InputMediaPhoto] = []
            for i, path in enumerate(paths):
                f = open(path, "rb")
                files.append(f)
                if first_batch and i == 0:
                    media_group.append(InputMediaPhoto(f, caption=caption_text))
                else:
                    media_group.append(InputMediaPhoto(f))
            return await bot.send_media_group(
                chat_id=chat_id,
                media=media_group,
                reply_to_message_id=reply_to_message_id,
            )
        except TelegramRetryAfter as e:
            last_exc = e
            delay: float = float(e.retry_after)
            logger.warning(
                f"Flood control (429) en media_group: esperando {delay}s "
                f"(intento {attempt+1}/{max_retries})"
            )
            await asyncio.sleep(delay)
        except (TelegramTimedOut, TelegramNetworkError) as e:
            last_exc = e
            delay: float = WORKER_RETRY_DELAY_BASE * (2 ** attempt)
            logger.warning(
                f"Reintento media_group {attempt+1}/{max_retries} en {delay}s: {e}"
            )
            await asyncio.sleep(delay)
        except Exception:
            raise
        finally:
            for f in files:
                try:
                    f.close()
                except Exception:
                    pass
    logger.error(f"Se agotaron los reintentos para media_group: {last_exc}")
    raise last_exc


def _find_downloaded_file(video_id: str, *extra_candidates: str) -> Optional[str]:
    """Localiza el archivo real que yt-dlp dejo en disco para un id de video.

    yt-dlp puede nombrar el resultado con extension .NA (HLS de Twitter/X),
    .mp4, .webm, etc. o, si falta ffmpeg, dejar los fragmentos sueltos. En vez
    de adivinar la extension, se barre el directorio de trabajo buscando
    cualquier archivo que contenga el id del video y se devuelve el mas grande
    (ignorando temporales .part/.ytdl).
    """
    candidates: set[str] = set()
    for c in extra_candidates:
        if c and os.path.isfile(c):
            candidates.add(c)

    try:
        for name in os.listdir("."):
            if video_id not in name:
                continue
            if name.endswith((".part", ".ytdl", ".temp", ".info.json", ".description")):
                continue
            full: str = os.path.join(os.getcwd(), name)
            if os.path.isfile(full):
                candidates.add(full)
    except OSError as e:
        logger.warning(f"No se pudo listar el directorio de descargas: {e}")

    existing: list[tuple[int, str]] = []
    for c in candidates:
        try:
            existing.append((os.path.getsize(c), c))
        except OSError:
            continue

    if not existing:
        # Diagnostico: volcar una muestra del directorio para entender por
        # que el sweep no encontro nada (ej. caso GIF de Twitter donde
        # yt-dlp descargo pero el archivo no aparecio aqui).
        try:
            sample: list[str] = sorted(os.listdir("."))[:20]
            logger.warning(
                f"Archivo con id '{video_id}' NO encontrado en {os.getcwd()} "
                f"({len(sample)}+ entradas): {sample}"
            )
        except OSError:
            pass
        return None
    return max(existing)[1]


def _ffmpeg_downscale_720(src: str) -> Optional[str]:
    """Re-encode un video a <=720p con ffmpeg (ultimo recurso del rescate 50MB).

    Copia el audio y re-encodea el video con CRF 28 / preset ultrafast: la
    CPU compartida del free tier es ~4-6x mas lenta que una local y con
    veryfast el encode superaba los 240s. ultrafast reduce el tiempo ~3x con
    archivos algo mayores; el techo de 720p mantiene el resultado bajo 50MB.
    Retorna la ruta nueva o None si ffmpeg no existe o falla.
    """
    ffmpeg: Optional[str] = shutil.which("ffmpeg")
    if not ffmpeg:
        return None
    dst: str = os.path.splitext(src)[0] + "_720.mp4"
    cmd = [
        ffmpeg, "-y", "-i", src,
        "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "copy",
        "-movflags", "+faststart",
        dst,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=420)
        if proc.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 0:
            logger.info(f"ffmpeg downscale OK: {dst} ({os.path.getsize(dst)} bytes)")
            return dst
        tail = (proc.stderr or b"")[-200:].decode("utf-8", "ignore")
        logger.warning(f"ffmpeg downscale rc={proc.returncode}: {tail}")
    except Exception as e:
        logger.warning(f"ffmpeg downscale fallo: {e}")
    return None


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

    # Pistas de Spotify: pipeline propio (match YT Music + MP3), sin yt-dlp video
    if task.meta and task.meta.get("kind") == "spotdl":
        await _execute_spotdl_task(task)
        return

    # Todas las resoluciones via HTTP corren en el executor: son llamadas
    # bloqueantes y este event loop es compartido por TODOS los usuarios.
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    # Resolver acortadores de Bilibili (b23.tv) y Niconico (nico.ms)
    url = await loop.run_in_executor(_download_executor, _resolve_short_url, url)

    # Resolver acortadores de TikTok (vt/vm.tiktok.com): yt-dlp debe recibir
    # la URL canonica (/video/ o /photo/) sin params de tracking (_r/_t), y
    # los fallbacks ya no necesitan re-resolverla.
    if is_tiktok:
        url = await loop.run_in_executor(_download_executor, _resolve_tiktok_url, url)

    # Limpiar URL de Reddit: _resolve_reddit_url elimina query params y
    # resuelve los acortadores /s/ a su forma canonica /comments/.
    if any(d in url for d in ["reddit.com", "redd.it"]):
        url = await loop.run_in_executor(_download_executor, _resolve_reddit_url, url)

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

    loop = asyncio.get_running_loop()

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
                        f"tiktok_slide_{uuid.uuid4().hex}_{i}.jpg",
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
                _record_result(url, False)
                return

            await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_PHOTO)
            caption_text: str = _build_caption(task)
            for batch_start in range(0, len(img_paths), 10):
                batch: list[str] = img_paths[batch_start : batch_start + 10]
                await _send_media_group_with_retry(
                    bot,
                    chat_id=task.chat_id,
                    paths=batch,
                    reply_to_message_id=task.message_id,
                    caption_text=caption_text,
                    first_batch=(batch_start == 0),
                )

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

            _record_result(url, True)
            return

    # ================================================================
    # Descarga normal con yt-dlp
    # ================================================================
    logger.info(f"Iniciando descarga yt-dlp para: {url}")

    def download(
        format_override: Optional[str] = None,
        cached_info: Optional[dict] = None,
    ) -> tuple[str, int, bool, str, dict]:
        """Funcion bloqueante que corre en el executor para no bloquear el event loop.

        Si se pasa cached_info (dict de una extraccion previa), se salta la
        re-extraccion y yt-dlp selecciona/descarga contra esos formatos:
        evita endpoints que throttlean peticiones repetidas (Reddit).
        """
        logger.debug("download: iniciando yt-dlp")
        opts: dict = get_ydl_opts()
        if format_override:
            opts["format"] = format_override
            # En rescates, check_formats prunea TODOS los formatos via HEAD
            # al CDN. Reddit rechaza esas HEADs segundos despues del primer
            # download y cualquier selector muere con "Requested format is
            # not available".
            opts["check_formats"] = False
            # format_sort dispara el error 'LazyList' object has no attribute
            # 'sort' al seleccionar sobre info cacheada: para un rescate da
            # igual el orden fino, basta con que entre algo que quepa.
            opts.pop("format_sort", None)
        with yt_dlp.YoutubeDL(opts) as ydl:
            if cached_info is not None:
                # Copia superficial manual: deepcopy del info completo falla
                # (contiene handles no picklables). Basta con clonar los
                # campos que process_ie_result muta durante la seleccion.
                info = dict(cached_info)
                info.pop("_format_sort_fields", None)
                info["formats"] = [dict(f) for f in cached_info.get("formats") or []]
                if cached_info.get("requested_downloads"):
                    info["requested_downloads"] = [
                        dict(r) for r in cached_info["requested_downloads"]
                    ]
                info = ydl.process_ie_result(info, download=True)
            else:
                info = ydl.extract_info(url, download=True)
            duration: int = info.get("duration", 0)
            is_video: bool = info.get("is_video", True) or bool(info.get("duration"))
            video_id: str = str(info.get("id", ""))

            # Reunir candidatos: prepare_filename + filepaths de yt-dlp.
            # Para HLS de Twitter/X la extension es .NA (desconocida) y para
            # descargas mergeadas el filepath de las partes se borra tras el
            # merge, por eso no se confia en una unica ruta.
            candidates: list[str] = []
            try:
                candidates.append(ydl.prepare_filename(info))
            except Exception:
                pass
            for rd in info.get("requested_downloads") or []:
                fp = rd.get("filepath")
                if fp:
                    candidates.append(fp)

            filename: Optional[str] = _find_downloaded_file(video_id, *candidates)
            if not filename:
                raise FileNotFoundError(
                    f"yt-dlp descargo el video '{video_id}' pero no se encontro "
                    f"el archivo resultante en {os.getcwd()}. Candidatos: {candidates}"
                )

            # HLS de Twitter/X deja la extension .NA (desconocida). Si es un
            # video, renombrar a .mp4 para que Telegram lo detecte correctamente.
            if is_video and os.path.splitext(filename)[1].lower() == ".na":
                mp4_name: str = os.path.splitext(filename)[0] + ".mp4"
                try:
                    os.rename(filename, mp4_name)
                    logger.info(f"Renombrado {filename} -> {mp4_name}")
                    filename = mp4_name
                except OSError as e:
                    logger.warning(f"No se pudo renombrar .NA a .mp4: {e}")

            logger.info(f"Archivo localizado para {video_id}: {filename}")
            return filename, duration, is_video, info.get("title") or "", info

    is_reddit: bool = any(d in url for d in ["reddit.com", "redd.it"])
    is_twitter: bool = any(d in url for d in ["twitter.com", "x.com"])
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
                if file_size > MAX_FILE_BYTES:
                    try:
                        os.remove(img_filename)
                    except OSError:
                        pass
                    await bot.edit_message_text(
                        chat_id=task.chat_id, message_id=task.processing_msg_id,
                        text="\u274c El archivo pesa mas de 50 MB."
                    )
                    _record_result(url, False)
                    return

                ext: str = os.path.splitext(img_filename)[1].lower()
                caption: str = _build_caption(task)
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
                        _record_result(url, False)
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
                _record_result(url, True)
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
                if file_size > MAX_FILE_BYTES:
                    try:
                        os.remove(tiktok_file)
                    except OSError:
                        pass
                    await bot.edit_message_text(
                        chat_id=task.chat_id, message_id=task.processing_msg_id,
                        text="\u274c El archivo pesa mas de 50 MB."
                    )
                    _record_result(url, False)
                    return

                caption = _build_caption(task)

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
                _record_result(url, True)
                try:
                    await bot.delete_message(chat_id=task.chat_id, message_id=task.processing_msg_id)
                except Exception:
                    pass
                return

            raise

    # Para Twitter/X: si no hay video, probar imagenes y GIFs del tweet
    if is_twitter:
        try:
            result = await loop.run_in_executor(_download_executor, download)
            downloaded = True
        except Exception as e:
            err_text: str = str(e)
            logger.info(f"Twitter: descarga de video fallo, probando media del tweet: {err_text[:200]}")
            media: Optional[tuple] = await loop.run_in_executor(
                _download_executor, _twitter_media_fallback, url
            )
            if media:
                img_urls, gif_urls = media
                caption_text = _build_caption(task)
                delivered: bool = False

                if img_urls:
                    img_paths: list[str] = await loop.run_in_executor(
                        _download_executor, _download_images, img_urls, "tw_img"
                    )
                    if img_paths:
                        await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_PHOTO)
                        for batch_start in range(0, len(img_paths), 10):
                            batch = img_paths[batch_start:batch_start + 10]
                            await _send_media_group_with_retry(
                                bot,
                                chat_id=task.chat_id,
                                paths=batch,
                                reply_to_message_id=task.message_id,
                                caption_text=caption_text,
                                first_batch=(batch_start == 0),
                            )
                        for p in img_paths:
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                        delivered = True

                if gif_urls:
                    # Los GIFs de X llegan como MP4 (tweet_video/*.mp4) y se
                    # envian via sendAnimation para que Telegram los reproduzca
                    # en loop en vez de mostrarlos como foto estatica.
                    gif_paths: list[str] = await loop.run_in_executor(
                        _download_executor,
                        lambda: _download_images(gif_urls, "tw_gif", default_ext=".mp4"),
                    )
                    for p in gif_paths:
                        try:
                            await _send_chat_action(bot, task.chat_id, ChatAction.UPLOAD_VIDEO)
                            await _send_file_with_retry(
                                bot, p,
                                lambda f: bot.send_animation(
                                    chat_id=task.chat_id,
                                    animation=f,
                                    caption=caption_text,
                                    reply_to_message_id=task.message_id,
                                    read_timeout=TG_READ_TIMEOUT,
                                    write_timeout=TG_WRITE_TIMEOUT,
                                    connect_timeout=TG_CONNECT_TIMEOUT,
                                ),
                            )
                            delivered = True
                        except Exception as gif_err:
                            logger.warning(f"GIF del tweet no enviado: {gif_err}")
                        finally:
                            try:
                                os.remove(p)
                            except OSError:
                                pass

                if delivered:
                    try:
                        await bot.delete_message(chat_id=task.chat_id, message_id=task.processing_msg_id)
                    except Exception:
                        pass
                    _record_result(url, True)
                    return
            raise

    # Twitter / Facebook (o Reddit/TikTok cuando download() tuvo exito)
    if not downloaded:
        result = await loop.run_in_executor(_download_executor, download)

    filename, duration, is_video, title, first_info = result
    file_size = os.path.getsize(filename)
    logger.info(f"Descarga completada: {filename} ({file_size} bytes)")

    if file_size > MAX_FILE_BYTES:
        logger.warning(
            f"Archivo excede {MAX_FILE_BYTES >> 20}MB ({file_size} bytes): "
            f"iniciando rescate con formatos reducidos"
        )
        try:
            await bot.edit_message_text(
                chat_id=task.chat_id,
                message_id=task.processing_msg_id,
                text="\u23f3 El archivo supera 50 MB. Reintentando en calidad reducida...",
            )
        except Exception:
            pass

        # CRITICO: renombrar (no borrar) el archivo grande a un nombre SIN el
        # video_id. Si se dejara con su nombre original, _find_downloaded_file
        # (que barre el cwd por video_id) recuperaria el grande viejo en cada
        # reintento; y si se borrara, el ultimo recurso ffmpeg quedaria sin
        # input. El nombre neutral "rescue_input_*" esquiva ambas trampas.
        oversized_path: Optional[str] = None
        try:
            oversized_path = os.path.join(
                os.path.dirname(filename) or ".",
                f"rescue_input_{uuid.uuid4().hex}.mp4",
            )
            os.rename(filename, oversized_path)
        except OSError as e:
            logger.warning(f"No se pudo renombrar el archivo grande: {e}")
            oversized_path = None

        rescued: Optional[tuple] = None
        for fmt in _DOWNLOAD_RESCUE_FORMATS:
            try:
                candidate = await loop.run_in_executor(
                    _download_executor,
                    lambda f=fmt: download(f, cached_info=first_info),
                )
                cand_file: str = candidate[0]
                size2: int = os.path.getsize(cand_file)
                logger.info(f"Rescate '{fmt[:40]}': {size2} bytes")
                if size2 <= MAX_FILE_BYTES:
                    rescued = candidate
                    break
                filename = cand_file
            except Exception as e:
                logger.warning(
                    f"Rescate con '{fmt[:40]}' fallo: {str(e)[:150]}"
                )

        if rescued is None and oversized_path and shutil.which("ffmpeg"):
            # Ultimo recurso: posts cuya unica variante excede 50MB (ej.
            # Reddit con solo 1080p). Re-encode del merge a <=720p con ffmpeg.
            logger.warning("Escalera de formatos agotada; probando re-encode ffmpeg 720p")
            try:
                await bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.processing_msg_id,
                    text="\u23f3 Convirtiendo a calidad reducida (puede tardar ~1-2 min)...",
                )
            except Exception:
                pass
            try:
                cand_file = await loop.run_in_executor(
                    _download_executor,
                    lambda: _ffmpeg_downscale_720(oversized_path),
                )
            except Exception as e:
                cand_file = None
                logger.warning(f"ffmpeg downscale fallo: {e}")
            if cand_file and os.path.isfile(cand_file):
                size3: int = os.path.getsize(cand_file)
                if size3 <= MAX_FILE_BYTES:
                    rescued = (cand_file, duration, True, title, first_info)
                    logger.info(f"Rescate ffmpeg exitoso: {size3} bytes")

        if rescued is None:
            if oversized_path:
                try:
                    os.remove(oversized_path)
                except OSError:
                    pass
            await bot.edit_message_text(
                chat_id=task.chat_id,
                message_id=task.processing_msg_id,
                text="\u274c El archivo pesa mas de 50 MB incluso en calidad reducida.\n"
                     "Telegram no permite enviar archivos tan grandes a traves de bots normales.",
            )
            _record_result(url, False)
            return

        filename, duration, is_video, title, _ = rescued

        if oversized_path and oversized_path != filename:
            try:
                os.remove(oversized_path)
            except OSError:
                pass

    caption = _build_caption(task, title)
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
            _record_result(url, False)
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
    _record_result(url, True)
    logger.info(f"Descarga exitosa para: {url}")

    try:
        await bot.delete_message(
            chat_id=task.chat_id,
            message_id=task.processing_msg_id,
        )
    except Exception:
        pass

# ============================================================
# Funciones de anime (APIs externas)
# ============================================================

_STATUS_ES: dict[str, str] = {
    "FINISHED": "Finalizado",
    "RELEASING": "En emision",
    "NOT_YET_RELEASED": "Por estrenar",
    "HIATUS": "En pausa",
    "CANCELLED": "Cancelado",
}


def _clean_html(text: str) -> str:
    """Elimina etiquetas HTML de las descripciones de AniList."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _translate_es(text: str) -> str:
    """Traduce un texto a espanol con el endpoint publico de Google Translate.

    Si falla, retorna el texto original para no perder la informacion.
    """
    if not text:
        return ""
    try:
        r = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "es", "dt": "t", "q": text[:4500]},
            timeout=HTTP_MEDIUM_TIMEOUT,
        )
        r.raise_for_status()
        parts = [seg[0] for seg in r.json()[0] if seg and seg[0]]
        return "".join(parts) if parts else text
    except Exception as e:
        logger.warning(f"Traduccion fallo: {e}")
        return text


def _anilist_query(query: str, variables: dict) -> dict:
    """Ejecuta una query GraphQL contra la API de AniList."""
    resp = requests.post(
        "https://graphql.anilist.co",
        json={"query": query, "variables": variables},
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
        timeout=HTTP_MEDIUM_TIMEOUT,
    )
    resp.raise_for_status()
    data: dict = resp.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"][0].get("message", "error de AniList")))
    return data.get("data", {})


def _search_anilist(kind: str, query: str) -> Optional[dict]:
    """Busca anime/manga en AniList y retorna el primer resultado."""
    media_type = "ANIME" if kind == "anime" else "MANGA"
    gql = """
    query ($s: String, $t: MediaType) {
      Media(search: $s, type: $t) {
        id
        title { romaji english native }
        description
        averageScore
        episodes
        chapters
        volumes
        status
        genres
        seasonYear
        coverImage { large }
        siteUrl
      }
    }
    """
    data = _anilist_query(gql, {"s": query, "t": media_type})
    return data.get("Media")


def _current_season() -> tuple[str, int]:
    """Devuelve la temporada actual de anime (WINTER/SPRING/SUMMER/FALL) y el anio."""
    t = time.gmtime()
    month, year = t.tm_mon, t.tm_year
    if month in (1, 2, 3):
        season = "WINTER"
    elif month in (4, 5, 6):
        season = "SPRING"
    elif month in (7, 8, 9):
        season = "SUMMER"
    else:
        season = "FALL"
    return season, year


def _seasonal_anime() -> list[dict]:
    """Retorna los animes mas populares de la temporada actual."""
    season, year = _current_season()
    gql = """
    query ($season: MediaSeason, $year: Int) {
      Page(page: 1, perPage: 10) {
        media(season: $season, seasonYear: $year, type: ANIME, sort: POPULARITY_DESC) {
          title { romaji english }
          averageScore
          episodes
        }
      }
    }
    """
    data = _anilist_query(gql, {"season": season, "year": year})
    return (data.get("Page") or {}).get("media") or []


def _upcoming_airing(hours: int = 24) -> list[dict]:
    """Retorna las emisiones de anime de las proximas horas (AniList)."""
    now = int(time.time())
    end = now + hours * 3600
    gql = """
    query ($start: Int, $end: Int) {
      Page(page: 1, perPage: 15) {
        airingSchedules(airingAt_greater: $start, airingAt_lesser: $end, sort: TIME) {
          episode
          airingAt
          media { title { romaji english } }
        }
      }
    }
    """
    data = _anilist_query(gql, {"start": now, "end": end})
    return (data.get("Page") or {}).get("airingSchedules") or []


def _random_waifu_url() -> Optional[str]:
    """Obtiene una URL de imagen waifu desde la API de nekos.best."""
    try:
        r = requests.get(
            "https://nekos.best/api/v2/waifu",
            headers={"User-Agent": "RandomBullshitDownloader/1.0 (personal telegram bot)"},
            timeout=HTTP_MEDIUM_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results") or []
        if results and results[0].get("url"):
            return results[0]["url"]
    except Exception as e:
        logger.warning(f"Waifu API nekos.best fallo: {e}")
    return None


_trace_lock: threading.Lock = threading.Lock()


def _trace_moe_search(image_bytes: bytes) -> tuple[Optional[dict], Optional[str]]:
    """Busca un anime por imagen en trace.moe (API oficial).

    Retorna (resultado, mensaje_de_error); el mensaje es None si hay exito.
    Usa cutBorders (recorta barras negras de screenshots) y anilistInfo
    (titulo exacto). Serializa las peticiones: el free tier tiene concurrencia 1.
    """
    if len(image_bytes) > 25 * 1024 * 1024:
        return None, "La imagen es demasiado grande (max 25 MB)."

    url = "https://api.trace.moe/search?cutBorders&anilistInfo"
    headers = {"User-Agent": "RandomBullshitDownloader/1.0 (personal telegram bot)"}

    for attempt in range(3):
        with _trace_lock:
            try:
                r = requests.post(
                    url,
                    files={"image": ("frame.jpg", image_bytes, "image/jpeg")},
                    headers=headers,
                    timeout=HTTP_LONG_TIMEOUT,
                )
            except Exception as e:
                logger.warning(f"trace.moe fallo: {e}")
                return None, "No pude conectar con trace.moe. Proba de nuevo mas tarde."

        if r.status_code == 200:
            data = r.json()
            results = data.get("result") or []
            if results:
                return results[0], None
            return None, "No encontre coincidencias para esa imagen."

        if r.status_code == 413:
            return None, "La imagen es demasiado grande (max 25 MB)."

        if r.status_code in (402, 429, 503):
            logger.warning(f"trace.moe {r.status_code}, reintento {attempt+1}/3")
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            return None, "trace.moe esta saturado o alcanzaste el limite de busquedas. Proba de nuevo mas tarde."

        logger.warning(f"trace.moe respondio {r.status_code}: {r.text[:200]}")
        return None, f"trace.moe respondio {r.status_code}. Proba de nuevo mas tarde."

    return None, "trace.moe no respondio correctamente."


def _fmt_relative(minutes: int) -> str:
    """Formatea minutos relativos en texto legible."""
    if minutes < 1:
        return "ahora"
    if minutes < 60:
        return f"en {minutes} min"
    h = minutes // 60
    m = minutes % 60
    return f"en {h}h {m:02d}m"


async def _reply_media(update: Update, info: dict, kind: str) -> None:
    """Envia la info de un anime/manga (portada + sinopsis traducida completa)."""
    title = (info.get("title") or {}).get("romaji") or (info.get("title") or {}).get("english") or "?"
    english = (info.get("title") or {}).get("english")
    lines: list[str] = [f"\U0001f4fa {title}"]
    if english and english != title:
        lines.append(f"\U0001f1ec\U0001f1e7 {english}")
    if info.get("averageScore"):
        lines.append(f"\u2b50 Score: {info['averageScore']}/100")
    if kind == "anime" and info.get("episodes"):
        lines.append(f"\U0001f3ac Episodios: {info['episodes']}")
    if kind == "manga":
        if info.get("chapters"):
            lines.append(f"\U0001f4d6 Capitulos: {info['chapters']}")
        if info.get("volumes"):
            lines.append(f"\U0001f4da Volumenes: {info['volumes']}")
    lines.append(f"\U0001f4cc Estado: {_STATUS_ES.get(info.get('status'), info.get('status') or '?')}")
    if info.get("seasonYear"):
        lines.append(f"\U0001f5d3 Anio: {info['seasonYear']}")
    genres = info.get("genres") or []
    if genres:
        lines.append(f"\U0001f3f7 Generos: {', '.join(genres[:5])}")

    caption = "\n".join(lines)
    cover = (info.get("coverImage") or {}).get("large")
    if cover:
        try:
            await _send_photo_url_safe(
                update.get_bot(), update.effective_chat.id, cover,
                caption=caption[:1000],
            )
        except Exception:
            await update.message.reply_text(caption, disable_web_page_preview=True)
    else:
        await update.message.reply_text(caption, disable_web_page_preview=True)

    # Sinopsis completa traducida al espanol (mensaje aparte para no recortarla)
    desc = _clean_html(info.get("description") or "")
    if desc:
        desc_es = await asyncio.get_running_loop().run_in_executor(
            _download_executor, _translate_es, desc
        )
        if len(desc_es) > 4000:
            desc_es = desc_es[:3997] + "..."
        await update.message.reply_text(f"\U0001f4dd Sinopsis:\n\n{desc_es}", disable_web_page_preview=True)


async def anime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca un anime en AniList."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Uso: /anime <nombre>\nEjemplo: /anime naruto")
        return
    await _send_chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING)
    info = await asyncio.get_running_loop().run_in_executor(_download_executor, _search_anilist, "anime", query)
    if not info:
        await update.message.reply_text("\u274c No encontre ese anime. Proba con otro nombre.")
        return
    await _reply_media(update, info, "anime")


async def manga_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Busca un manga en AniList."""
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Uso: /manga <nombre>\nEjemplo: /manga berserk")
        return
    await _send_chat_action(context.bot, update.effective_chat.id, ChatAction.TYPING)
    info = await asyncio.get_running_loop().run_in_executor(_download_executor, _search_anilist, "manga", query)
    if not info:
        await update.message.reply_text("\u274c No encontre ese manga. Proba con otro nombre.")
        return
    await _reply_media(update, info, "manga")


async def temporada_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra los animes mas populares de la temporada actual."""
    await _send_chat_action(update.get_bot(), update.effective_chat.id, ChatAction.TYPING)
    media = await asyncio.get_running_loop().run_in_executor(_download_executor, _seasonal_anime)
    if not media:
        await update.message.reply_text("\u274c No pude obtener la temporada actual.")
        return
    season, year = _current_season()
    lines = [f"\U0001f331 Temporada {season} {year} — Top 10:\n"]
    for i, m in enumerate(media, 1):
        t = (m.get("title") or {}).get("romaji") or (m.get("title") or {}).get("english") or "?"
        score = m.get("averageScore") or "?"
        ep = m.get("episodes") or "?"
        lines.append(f"{i}. {t} — \u2b50{score} — {ep} ep")
    await update.message.reply_text("\n".join(lines))


async def hoy_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Muestra las emisiones de anime de las proximas 24h."""
    await _send_chat_action(update.get_bot(), update.effective_chat.id, ChatAction.TYPING)
    sched = await asyncio.get_running_loop().run_in_executor(_download_executor, _upcoming_airing, 24)
    if not sched:
        await update.message.reply_text("\U0001f4e1 No hay emisiones en las proximas 24h.")
        return
    lines = ["\U0001f4e1 Emisiones proximas (24h):\n"]
    for s in sched:
        m = s.get("media") or {}
        t = (m.get("title") or {}).get("romaji") or (m.get("title") or {}).get("english") or "?"
        ep = s.get("episode")
        airing_at = s.get("airingAt", 0)
        when = _fmt_relative(int((airing_at - time.time()) // 60))
        lines.append(f"\u2022 {t} — Ep {ep if ep else '?'} — {when}")
    await update.message.reply_text("\n".join(lines))


async def waifu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia una imagen random de waifu."""
    await _send_chat_action(context.bot, update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
    url = await asyncio.get_running_loop().run_in_executor(_download_executor, _random_waifu_url)
    if not url:
        await update.message.reply_text("\u274c No pude conseguir una waifu ahora. Proba de nuevo.")
        return
    await _send_photo_url_safe(
        context.bot, update.effective_chat.id, url,
        caption="Aqui tienes tu waifu \U0001f458",
    )


async def identify_anime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Identifica un anime a partir de un screenshot usando trace.moe."""
    if not update.message or not update.message.photo:
        return
    photo = update.message.photo[-1]
    msg = await update.message.reply_text("\U0001f50d Identificando anime...")
    try:
        f = await context.bot.get_file(photo.file_id)
        data = await f.download_as_bytearray()
    except Exception as e:
        logger.warning(f"No se pudo descargar la imagen para trace.moe: {e}")
        await msg.edit_text("\u274c No pude descargar la imagen.")
        return
    result, error = await asyncio.get_running_loop().run_in_executor(
        _download_executor, _trace_moe_search, bytes(data)
    )
    if error:
        await msg.edit_text(f"\u274c {error}")
        return

    # Titulo: preferir anilistInfo (objeto) y caer al parseo del filename
    anilist_info = result.get("anilist")
    if isinstance(anilist_info, dict):
        t = anilist_info.get("title") or {}
        anime_title = t.get("romaji") or t.get("english") or t.get("native") or "Desconocido"
    else:
        filename = result.get("filename") or ""
        anime_title = filename.rsplit(" - ", 1)[0] if " - " in filename else (filename or "Desconocido")

    ep = result.get("episode")
    similarity = round((result.get("similarity") or 0) * 100, 1)
    at_sec = result.get("at") or result.get("from") or 0
    ts = f"{int(at_sec // 60)}:{int(at_sec % 60):02d}"
    caption = (
        f"\U0001f3cc {anime_title}\n"
        f"\U0001f4fa Episodio: {ep if ep else '?'}\n"
        f"\u23f1 Tiempo: ~{ts}\n"
        f"\U0001f3af Similitud: {similarity}%"
    )
    if similarity < 90:
        caption += "\n\u26a0\ufe0f Resultado poco confiable (similitud baja)"

    preview = result.get("image")
    if preview:
        try:
            await _send_photo_url_safe(
                context.bot,
                update.effective_chat.id,
                preview,
                caption=caption,
                reply_to=update.message.message_id,
            )
            await msg.delete()
            return
        except Exception:
            pass
    await msg.edit_text(caption)


# ============================================================
# Registro de handlers de Telegram
# ============================================================
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_cmd))
application.add_handler(CommandHandler("id", show_id))
application.add_handler(CommandHandler("queue", queue_cmd))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("cancel", cancel))
application.add_handler(CommandHandler("config", config_cmd))
application.add_handler(CallbackQueryHandler(config_cb, pattern=r"^cfg:"))
application.add_handler(CallbackQueryHandler(spot_cb, pattern=r"^spt:"))
application.add_handler(CommandHandler("anime", anime_cmd))
application.add_handler(CommandHandler("manga", manga_cmd))
application.add_handler(CommandHandler("temporada", temporada_cmd))
application.add_handler(CommandHandler("hoy", hoy_cmd))
application.add_handler(CommandHandler("waifu", waifu_cmd))
application.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, download_video)
)
application.add_handler(MessageHandler(filters.PHOTO, identify_anime))

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
            allowed_updates=["message", "callback_query"],
            max_connections=40,
            drop_pending_updates=True,
        )
        logger.info(f"Webhook configurado en {webhook_url}")

    # Registrar el menu de comandos nativo de Telegram
    await application.bot.set_my_commands([
        BotCommand("start", "Mensaje de bienvenida"),
        BotCommand("help", "Ayuda y plataformas soportadas"),
        BotCommand("cancel", "Cancelar descargas pendientes"),
        BotCommand("config", "Configura credito y titulo de descargas"),
        BotCommand("queue", "Ver tu cola de descargas"),
        BotCommand("id", "Ver tus IDs de chat/usuario"),
        BotCommand("stats", "Estadisticas (solo admins)"),
        BotCommand("anime", "Buscar info de un anime"),
        BotCommand("manga", "Buscar info de un manga"),
        BotCommand("temporada", "Animes de la temporada actual"),
        BotCommand("hoy", "Emisiones de las proximas 24h"),
        BotCommand("waifu", "Imagen random de waifu"),
    ])
    logger.info("Menu de comandos configurado")


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
            threading.Thread(target=_restore_and_start_flusher, daemon=True).start()
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
    try:
        payload: dict = request.get_json(force=True)
    except Exception as e:
        logger.warning(f"Webhook con JSON invalido: {e}")
        return "Bad Request", 400
    update: Update = Update.de_json(payload, application.bot)
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
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "deno": bool(shutil.which("deno")),
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
