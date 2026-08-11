# AGENTS.md — DownloaderVideosBot

System-level instructions and architectural guidelines for **DownloaderVideosBot**, an asynchronous Telegram Bot written in Python for media downloading.

---

## 🛠️ Stack & Architecture

- **Runtime:** Python 3.11+ using native `asyncio`.
- **Telegram Framework:** `python-telegram-bot` v22 (Application API utilizing Webhooks).
- **Core Downloader:** `yt-dlp` (Stream selection priority: `bestvideo[filesize<50M]+bestaudio/best[filesize<50M]/bestvideo+bestaudio/best`).
- **Web Server:** `Flask` (webhook handler) running under `Gunicorn` (1 worker, 2 threads).
- **Deployment:** Render (configured via `Procfile`, `requirements.txt`, and `runtime.txt`).
- **File Structure:** Single-file monolithic script (`main.py`) for clean, self-contained deployment.
- **Admin Commands:** `/stats` for admins (configured via `ADMIN_IDS` env var).

---

## 🔄 Core Workflow & Logic

1. **Input Detection:** Monitor all text messages. Validate domains against known TikTok, Facebook, Twitter/X, and Reddit patterns. Guard against `update.message` being `None` (e.g. callback queries, channel posts).
2. **Instagram not supported:** Instagram was removed by decision (anonymously blocked by Instagram). Do not re-add it.
3. **Queue System (FIFO per user):** Each URL received is validated and then enqueued into a per-user `asyncio.Queue`. A dedicated background worker (`_queue_worker`) processes tasks in order. Workers auto-terminate after 5 minutes of inactivity.
4. **TikTok Slideshows:** Detect `/photo/` URLs. Bypass `yt-dlp` and utilize the `tikwm.com` API as a robust fallback.
5. **Download Phase:** Initialize `yt-dlp`. The output is written to a temp file in `tempfile.gettempdir()`. No progress is shown to the user — only a static ⏳ emoji.
6. **Reddit Image/GIF Fallback:** When yt-dlp's first download attempt fails, the error message is parsed to extract the media URL directly (from `reddit.com/media?url=...` or `i.redd.it/...` URLs in the error text). If parsing fails, a multi-strategy fallback resolves `/s/` → `/comments/` then tries: (1) yt-dlp `process=False`, (2) Reddit oembed API, (3) Reddit JSON API. The image/GIF is downloaded directly with `requests` and sent via Telegram.
7. **Delivery Phase with Retry:** Upload via Telegram (`HTTPXRequest` defaults: `connect_timeout=30`, `read_timeout=120`, `write_timeout=30`, `media_write_timeout=300`, `pool_timeout=30`, `connection_pool_size=256`; per-call overrides use `read_timeout=120`, `write_timeout=120`, `connect_timeout=30`). File limits: videos/GIFs `< 50 MB`, photos `< 10 MB` (Telegram rejects bigger photos). All `send_video`/`send_animation`/`send_photo` calls are wrapped in `_send_file_with_retry()` which re-opens the file and retries up to 3 times on `TimedOut`/`NetworkError` with exponential backoff. If the file has no audio stream, it is sent as `send_animation` (GIF) instead of `send_video`. All uploads use `application.bot.send_*()` (not `update.message.reply_*()`) because the download runs in the queue worker context. **Chat Action:** Before each upload, `_send_chat_action()` is called (throttled to max 1 per ~4.5s per chat) with the appropriate action (`UPLOAD_VIDEO` for videos/GIFs, `UPLOAD_PHOTO` for photos/albums) so Telegram shows a "subiendo..." status to the user.
8. **Garbage Collection:** Always clean up temporary files, even if the upload fails.
9. **Metrics:** Track total requests, successful downloads, failed downloads, and unique users in a thread-safe global dict (`_stats` with `_stats_lock`).

---

## ⚙️ Hard Rules & Network Tunings

When writing or refactoring code, you must strictly adhere to these parameters:

### yt-dlp Configuration

- `socket_timeout`: `120` seconds.
- `extractor_retries`: `3` attempts.
- `file_access_retries`: `3` attempts.
- `retries`: `5` (HTTP retries, default is 10, lowered to avoid waiting too long on unresponsive servers).
- `retry_sleep`: `{"extractor": "linear=1:5"}` (backoff lineal 1-5s solo para retries del extractor, sin afectar los de red/file).
- `concurrent_fragments`: `3` (downloads up to 3 fragments in parallel for DASH/HLS videos, speeding up downloads).
- `check_formats`: `True` (verifies formats are actually accessible before downloading, reducing mid-download failures).
- `ratelimit`: `10 * 1024 * 1024` (10 MB/s max to avoid saturating Render's shared bandwidth).
- `noplaylist`: `True` (evita galerias multiples en Reddit).
- `outtmpl`: `"%(title).100B [%(id)s].%(ext)s"` (trunca el titulo por BYTES, no caracteres: un titulo CJK de 180 caracteres = ~540 bytes y excede el limite de 255 bytes del filesystem -> ENAMETOOLONG. 100 bytes + sufijo queda siempre < 255).
- `trim_filenames`: `180` (respaldo adicional contra "File name too long").
- `sleep_interval_requests`: `1` (pausa de 1s entre requests de info: anti-429 en Twitter/TikTok).
- `extractor_args`: `{"generic": {"impersonate": ["chrome"]}, "tiktok": {"app_version": ["35.1.3"], "manifest_app_version": ["2023501030"], "app_name": ["musical_ly"]}}`.
- `embedthumbnail`: `True` (best-effort embedding of thumbnail into the video as cover art via ffmpeg).
- `merge_output_format`: Always force `mp4` for standard Telegram client playback compatibility.
- `cookies`: Optionally load from a file path defined in `COOKIES_FILE` environment variable, falling back to `tempfile/cookies.txt`.
- `cachedir`: Always set to `YDL_CACHE_DIR` env var (or `<tempdir>/ydl_cache`). This enables yt-dlp's built-in extractor cache to avoid re-fetching info for repeated URLs.
- `impersonate`: NOT set globally in `get_ydl_opts()` because it breaks extractors on Render. Only set per-extractor via `extractor_args["generic"]["impersonate"] = ["chrome"]` for the generic extractor (Facebook links via Cloudflare). In `_get_reddit_media_url()` impersonate is set to `""` ("any target") for Reddit-specific calls.

### Queue System

- **Per-user FIFO queue:** Each user has their own `asyncio.Queue[DownloadTask]`. Workers are created on-demand and auto-terminate after 5 minutes of inactivity.
- **Executor:** A shared `ThreadPoolExecutor(max_workers=2)` handles yt-dlp operations across all queues.
- **Error isolation:** Errors in `_execute_download` are caught by the worker and reported to the user via the processing message, without crashing the worker.

### Metrics & Admin

- **`/stats` command:** Accessible only to user IDs listed in `ADMIN_IDS` env var (comma-separated). Shows uptime, total requests, successes, failures, unique users, and active queues.
- **Global stats dict (`_stats`):** Thread-safe (`_stats_lock`). Tracks `start_time`, `total_requests`, `successful`, `failed`, `unique_users`.
- **Health endpoint (`/health`):** ALWAYS returns `200` while the process is alive (Render restarts after consecutive 5xx/4xx). Readiness is exposed as a JSON field: `status` (`ok`/`starting`), `yt_dlp_version`, `bot_ready`, `queues`. Never return 503 from `/health`.

### Webhook & Telegram API

- **Webhook security:** `set_webhook()` includes `secret_token=TELEGRAM_WEBHOOK_SECRET` (env `TELEGRAM_WEBHOOK_SECRET`). The Flask `/webhook` route MUST reject requests whose `X-Telegram-Bot-Api-Secret-Token` header does not match (403) — but only when the env var is set.
- **Bootstrap order in `/webhook`:** `ensure_bot()` MUST run BEFORE the secret check. If Telegram still has an OLD webhook config (no secret), its first update arrives without the header; that request must bootstrap the bot (which re-runs `set_webhook` with the secret) and then return 403. Telegram retries the SAME update already with the header → it passes. Never gate the first initialization behind the secret check (that is a permanent 403 deadlock).
- **Single persistent event loop:** `ensure_bot()` starts ONE daemon thread that runs the bot's event loop for the whole process lifetime. Init is retried on the SAME loop if it fails. Never create a new loop per attempt — the shared `HTTPXRequest` client binds to the first loop and later attempts fail with "bound to a different event loop".
- **gunicorn runs with `--preload` (Render):** the module is imported in the MASTER, then workers are forked. Therefore NEVER do side effects at module import time (no eager `threading.Thread(target=ensure_bot)` at the bottom of the file) — the master's loop/thread would be inherited broken by the forked workers (`_bot_ready=False` + dead loop → webhook hangs on `future.result(60)`). Init MUST be triggered from a Flask request (worker context): `/health` fires a fire-and-forget `ensure_bot()` thread when `not _bot_ready`, and `/webhook` calls it synchronously before the secret check.
- **Webhook tuning:** `allowed_updates=["message"]`, `drop_pending_updates=True`, `max_connections=40`.
- **HTTPXRequest:** The `Application` is built with a shared `HTTPXRequest` (`connect_timeout=30`, `read_timeout=120`, `write_timeout=30`, `media_write_timeout=300`, `pool_timeout=30`, `connection_pool_size=256`). `media_write_timeout` is the one that matters for big uploads (default 20s would break 50MB files).
- **Chat actions:** Use the `_send_chat_action()` helper (throttled, max 1/~4.5s per chat) — never call `bot.send_chat_action()` directly.

### UX & Error Handling

- **State management:** Use a single "⏳" status message. Show "⏳ En cola..." initially, then edit to "⏳" when processing starts. Always **edit** this message to show failure. Never edit it to show download progress. Never send duplicate/spammy error messages.
- **Error translations:** Catch known `yt-dlp` exceptions (e.g., Geo-restriction, private video, deleted content) and map them to friendly, localized Spanish errors instead of throwing raw stack traces to the user.
- **Empty errors:** If `str(e)` is empty (no error text), the bot generates a contextual message based on the platform domain (TikTok → "privado/cambio en el sitio"). The full traceback is logged for debugging.
- **Start message sync:** Whenever a new platform or feature is added (e.g., GIF support, a new domain, queue system), the `/start` command text must be updated to reflect it. This ensures users always see an accurate list of supported platforms and capabilities.

### Environment Variables

| Variable                   | Required | Default                 | Description                                      |
| -------------------------- | -------- | ----------------------- | ------------------------------------------------ |
| `TELEGRAM_TOKEN`           | ✅       | —                       | Bot token de Telegram                            |
| `TELEGRAM_WEBHOOK_SECRET`  | ❌       | —                       | Secreto para validar el webhook (recomendado)    |
| `RENDER_EXTERNAL_HOSTNAME` | ❌       | —                       | URL externa de Render (para webhook)             |
| `ADMIN_IDS`                | ❌       | —                       | IDs de Telegram separados por coma para `/stats` |
| `COOKIES_FILE`             | ❌       | `<tempdir>/cookies.txt` | Ruta al archivo de cookies                       |
| `YDL_CACHE_DIR`            | ❌       | `<tempdir>/ydl_cache`   | Directorio para caché de extractores de yt-dlp   |
| `MAX_URLS_PER_MESSAGE`     | ❌       | `20`                    | Máximo de URLs permitidas por mensaje            |
| `PORT`                     | ❌       | `8080`                  | Puerto del servidor Flask                        |

### Render / Despliegue

- **yt-dlp se instala en el BUILD, no en el start.** El `Procfile` solo corre `gunicorn`; el nightly/master se instala en el Build Command: `pip install -r requirements.txt && pip install -U --force-reinstall https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz`. Esto evita retrasos de readiness en cada deploy/restart/spin-up y no quema horas del free tier.
- **Gunicorn:** 1 worker, 2 threads, `--timeout 300`, `--keep-alive 5`. PTB procesa updates en serie y no es thread-safe; mas threads solo consumen RAM (512MB).
- **SIGTERM:** NO interceptar. Gunicorn gestiona el ciclo de vida del worker; el `ThreadPoolExecutor` se autolimpia via el atexit de `concurrent.futures`, y el event loop del bot corre en un thread daemon. Un handler propio con `executor.shutdown(wait=True)` + `os._exit(0)` bloquea o corta requests en vuelo.
- **Health check:** configurar `Health Check Path` de Render a `/health` (siempre 200).

---

## 💻 Coding Standards (For the AI Agent)

- **Async First:** Avoid blocking the event loop. Always wrap synchronous blocking calls (like file system writes or `yt-dlp` invocations) using `loop.run_in_executor()` with `_download_executor`.
- **Conciseness:** Provide direct code updates. When modifying `main.py`, output the specific changed function or block rather than rewriting the entire file, unless explicitly requested.
- **Language & Documentation:** Maintain all user-facing bot messages, code comments, and chat explanations in **Spanish**. All code must include clear comments documenting its purpose and logic in Spanish.
- **Logging Required:** Every function must include logs (`logging.info`, `logging.warning`, `logging.error`) to record its entry, key decisions, and errors. This allows tracking the bot's flow and diagnosing issues without debugging in production.
- **Thread safety:** Global mutable state (`_stats`, `_user_queues`, `_queue_workers`) must be protected. Use `threading.Lock` for dict/set mutations accessed from multiple threads.
- **Download handler pattern:** `download_video` is a thin handler that validates + enqueues. The actual download logic lives in `_execute_download`. Errors in `_execute_download` are caught by `_queue_worker` which handles user-friendly error messages and metric tracking.

---

## 🧪 Testing & Verification Protocol

Before submitting any change to the codebase, the agent MUST follow this protocol:

### 1. Test Every Function

Every new or modified function must be tested by running the code locally via Python:

- Run `python -c "import ast; ast.parse(open('main.py', encoding='utf-8').read())"` to verify syntax.
- Verify all imports work (`python -c "import yt_dlp, telegram, flask, requests"`).
- Ensure no dead code (unused imports, variables, etc.) is left behind.

### 2. Verify Every User URL

When the user reports an issue with a specific URL:

1. **Test the URL locally** with the exact yt-dlp command used in the bot:
   ```
   yt-dlp --format "best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best" --socket-timeout 120 --merge-output-format mp4 <URL>
   ```
2. **Identify the root cause** — is it a network issue, a yt-dlp extractor limitation, a format mismatch, or a bot bug?
3. **Apply a fix** in the bot code if the issue is on our side. If the issue is in yt-dlp or the platform (e.g., Twitter API change, TikTok blocking), **do not silently ignore it** — explain to the user what the limitation is and offer alternatives (e.g., use cookies, update yt-dlp, report upstream).

### 3. Classify Errors Correctly

- If the error is in **yt-dlp**, `python-telegram-bot`, or another library → explain it clearly and provide workarounds if available.
- If the error is from the **platform** (Twitter/X, Instagram, TikTok blocking/rate-limiting) → explain the limitation and what the user can do (cookies, retry later, etc.).
- If the error is a **bug in the bot code** → fix it directly.
- Never suggest unrelated solutions (e.g., `@botfather` for download issues).

### 4. Verify Before Deploying

After any code change, run a final syntax and import check before telling the user the fix is ready.

### 5. Render Compatibility Check

Before deploying any new feature or platform integration to production, verify that it will work in Render's environment:

- **Datacenter IP:** Render uses datacenter IPs. Some platforms (like YouTube) block these IPs. Do not add a platform if it requires a residential IP or cookies to function.
- **Disk space:** Render has an ephemeral filesystem. Make sure to use `tempfile.gettempdir()` for temporary files (already implemented).
- **Gunicorn timeout:** The `Procfile` has `--timeout 300`. Any operation exceeding this time must run in the background.
- **Cookies:** If a platform mandatorily requires cookies on Render, document it in the `/start` command as a known limitation before adding it.
- **Test simulating Render:** If possible, test the URL with yt-dlp from a VPS or cloud server (not just from a residential IP) to confirm it works.

Do not approve a new feature if it does not pass this Render verification.
