import os
import re
import json
import time
import asyncio
import threading
import tempfile
import logging
import subprocess
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
import requests

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

    def hook(self, d):
        if d["status"] == "downloading":
            try:
                raw = d.get("_percent_str", "").strip().replace("%", "")
                pct = float(raw)
                if pct - self.last_pct >= 5 or (pct == 100 and self.last_pct != 100):
                    self.last_pct = pct
                    asyncio.run_coroutine_threadsafe(
                        self.message.edit_text(f"⏳ Descargando... {pct:.0f}%"),
                        self.loop,
                    )
            except Exception:
                pass

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

def combine_image_audio(image_path, audio_path, output_path):
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True, timeout=30
    )
    duration = float(probe.stdout.strip())
    subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-t", str(duration), "-shortest", output_path],
        capture_output=True, timeout=300
    )
    return output_path if os.path.exists(output_path) else None

async def tiktok_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = f"tiktok:{query.message.message_id}"
    stored = context.user_data.pop(key, None)
    if not stored:
        await query.edit_message_text("❌ Esta solicitud ya expiró.")
        return
    url = stored["url"]
    info = stored["info"]
    loop = asyncio.get_running_loop()

    if query.data == "tiktok_image":
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
                caption=f"📥 Descargado por @{stored['context'].bot.username}\n🔗 {url}",
            )
        os.remove(filename)
        return

    await query.edit_message_text("⏳ Descargando imagen y audio...")
    sf = stored["slideshow_format"]
    af = stored["audio_format"]
    img_url = sf.get("url")
    audio_url = af.get("url")
    if not img_url or not audio_url:
        await query.edit_message_text("❌ No se pudieron obtener las URLs de descarga.")
        return
    img_ext = sf.get("ext", "jpg")
    audio_ext = af.get("ext", "m4a")
    ts = int(time.time())
    img_path = os.path.join(tempfile.gettempdir(), f"tiktok_img_{ts}.{img_ext}")
    audio_path = os.path.join(tempfile.gettempdir(), f"tiktok_audio_{ts}.{audio_ext}")

    def dl_both():
        r1 = requests.get(img_url, timeout=60)
        r1.raise_for_status()
        with open(img_path, "wb") as f:
            f.write(r1.content)
        r2 = requests.get(audio_url, timeout=120)
        r2.raise_for_status()
        with open(audio_path, "wb") as f:
            f.write(r2.content)

    await loop.run_in_executor(None, dl_both)

    await query.edit_message_text("⏳ Combinando imagen con audio...")
    output = os.path.join(tempfile.gettempdir(), f"tiktok_video_{ts}.mp4")
    result = await loop.run_in_executor(None, combine_image_audio, img_path, audio_path, output)
    os.remove(img_path)
    os.remove(audio_path)
    if not result:
        await query.edit_message_text("❌ Error al crear el video.")
        return

    await query.edit_message_text("📤 Subiendo a Telegram...")
    with open(output, "rb") as f:
        await query.message.reply_video(
            video=f,
            caption=f"📥 Descargado por @{stored['context'].bot.username}\n🔗 {url}",
            supports_streaming=True,
        )
    os.remove(output)

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

        # --- TikTok slideshow detection ---
        if is_tiktok:
            def extract_info():
                with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
                    return ydl.extract_info(url, download=False)

            info = await loop.run_in_executor(None, extract_info)
            formats = info.get("formats", [])
            slideshow_formats = [f for f in formats if f.get("format_id", "").startswith("slideshow-")]

            if slideshow_formats:
                audio_formats = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec", "none") != "none"]

                if len(slideshow_formats) == 1 and audio_formats:
                    await processing_msg.edit_text("🖼️ TikTok: foto con audio.")
                    keyboard = [[
                        InlineKeyboardButton("🎬 Video (foto+audio)", callback_data="tiktok_video"),
                        InlineKeyboardButton("🖼️ Solo imagen", callback_data="tiktok_image"),
                    ]]
                    ask_msg = await update.message.reply_text(
                        "¿Cómo quieres descargarlo?",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                    context.user_data[f"tiktok:{ask_msg.message_id}"] = {
                        "url": url,
                        "info": info,
                        "slideshow_format": slideshow_formats[0],
                        "audio_format": audio_formats[0],
                        "context": context,
                    }
                    return

                await processing_msg.edit_text("⏳ Descargando imágenes...")

                def dl_slideshow():
                    paths = []
                    for sf in slideshow_formats:
                        img_url = sf.get("url")
                        if not img_url:
                            continue
                        ext = sf.get("ext", "jpg")
                        path = os.path.join(
                            tempfile.gettempdir(),
                            f"tiktok_slide_{int(time.time())}_{sf['format_id']}.{ext}",
                        )
                        r = requests.get(img_url, timeout=60)
                        r.raise_for_status()
                        with open(path, "wb") as f:
                            f.write(r.content)
                        paths.append(path)
                    return paths

                img_paths = await loop.run_in_executor(None, dl_slideshow)
                if not img_paths:
                    await processing_msg.edit_text("❌ No se pudieron descargar las imágenes.")
                    return

                await processing_msg.edit_text("📤 Subiendo a Telegram...")
                media_group = []
                for i, path in enumerate(img_paths):
                    with open(path, "rb") as f:
                        if i == 0:
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

        result = await loop.run_in_executor(None, download)
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
application.add_handler(CommandHandler("blacklist", blacklist_cmd))
application.add_handler(CallbackQueryHandler(tiktok_callback, pattern="^tiktok_"))
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
