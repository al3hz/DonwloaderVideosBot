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
from flask import Flask, request
from telegram import Update, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import requests 

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
BLACKLIST_FILE = os.path.join(tempfile.gettempdir(), "blacklist.json")

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
        await update.message.reply_text("Uso: /blacklist — ver lista\n/blacklist add <dominio>\n/blacklist remove <dominio>")

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
        "• Instagram (Reels)\n"
        "• Facebook\n"
        "• Y muchas más...\n\n"
        "⚠️ Límite: 50 MB por archivo (límite de Telegram para bots).\n\n"
        "📋 Comandos:\n"
        "  /blacklist — Ver /blacklist add <dominio> /blacklist remove <dominio>"
    )
    await update.message.reply_text(text)

class ProgressTracker:
    def __init__(self, loop, message):
        self.loop = loop
        self.message = message
        self.last_pct = -1
        self._closed = False

    def hook(self, d):
        if d["status"] == "downloading":
            try:
                raw = d.get("_percent_str", "").strip().replace("%", "")
                pct = float(raw)
                if pct - self.last_pct >= 5 or (pct == 100 and self.last_pct != 100):
                    self.last_pct = pct
                    if not self._closed and self.loop and not self.loop.is_closed():
                        asyncio.run_coroutine_threadsafe(
                            self.message.edit_text(f"⏳ Descargando... {pct:.0f}%"),
                            self.loop,
                        )
            except Exception:
                pass

    def close(self):
        self._closed = True

def get_ydl_opts(progress_hook=None):
    opts = {
        "format": "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best",
        "outtmpl": os.path.join(tempfile.gettempdir(), "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]
    return opts

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

async def download_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Envía un enlace válido.")
        return

    blacklist = load_blacklist()
    if any(domain in url.lower() for domain in blacklist):
        await update.message.reply_text("🚫 Este dominio está en la lista negra y no puede descargarse.")
        return

    processing_msg = await update.message.reply_text("⏳ Analizando enlace...")

    try:
        loop = asyncio.get_running_loop()
        is_instagram = "instagram.com" in url
        is_tiktok = "tiktok.com" in url

        # --- TikTok photos (via API) ---
        if is_tiktok and "/photo/" in url:
            api_data = await loop.run_in_executor(_download_executor, _tiktok_api_fallback, url)
            if api_data:
                slideshow_formats, _, images = api_data

                def dl_slideshow():
                    paths = []
                    for i, img_url in enumerate(images):
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
                if img_paths:
                    for batch_start in range(0, len(img_paths), 10):
                        batch = img_paths[batch_start:batch_start + 10]
                        media_group = []
                        for i, path in enumerate(batch):
                            with open(path, "rb") as f:
                                if batch_start == 0 and i == 0:
                                    media_group.append(
                                        InputMediaPhoto(f, caption=f"📥 Descargado por @{context.bot.username}\n🔗 {url}")
                                    )
                                else:
                                    media_group.append(InputMediaPhoto(f))
                        await update.message.reply_media_group(media_group)
                    for p in img_paths:
                        os.remove(p)
                    return

        # --- Normal download ---
        tracker = ProgressTracker(loop, processing_msg)

        def download():
            if is_instagram:
                try:
                    fname, ftitle = download_instagram_photo(url)
                    return fname, ftitle, 0, False, True
                except Exception:
                    pass
            opts = get_ydl_opts(tracker.hook)
            with yt_dlp.YoutubeDL(opts) as ydl:
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

        size_mb = file_size / 1024 / 1024
        base_text = f"📤 Subiendo {size_mb:.1f} MB"

        async def animate():
            dots = 0
            while True:
                await processing_msg.edit_text(f"{base_text}{'.' * dots}")
                dots = (dots + 1) % 4
                await asyncio.sleep(1)

        anim_task = asyncio.create_task(animate())

        try:
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
        finally:
            anim_task.cancel()

        os.remove(filename)

    except yt_dlp.utils.DownloadError as e:
        try:
            await processing_msg.edit_text(f"❌ Error de descarga:\n`{str(e)}`", parse_mode="Markdown")
        except Exception:
            pass
        try:
            await update.message.reply_text(f"❌ Error de descarga:\n`{str(e)}`", parse_mode="Markdown")
        except Exception:
            pass
    except Exception as e:
        msg = str(e)[:200]
        try:
            await processing_msg.edit_text(f"❌ Error inesperado:\n`{msg}`", parse_mode="Markdown")
        except Exception:
            pass
        try:
            await update.message.reply_text(f"❌ Error inesperado:\n`{msg}`", parse_mode="Markdown")
        except Exception:
            pass
    else:
        await processing_msg.delete()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("blacklist", blacklist_cmd))
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
