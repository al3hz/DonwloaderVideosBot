# AGENTS.md — DownloaderVideosBot

System-level instructions and architectural guidelines for **DownloaderVideosBot**, an asynchronous Telegram Bot written in Python for media downloading.

---

## 🛠️ Stack & Architecture

- **Runtime:** Python 3.11+ using native `asyncio`.
- **Telegram Framework:** `python-telegram-bot` v22 (Application API utilizing Webhooks).
- **Core Downloader:** `yt-dlp` (Stream selection priority: `best[filesize<50M]/bestvideo[filesize<50M]+bestaudio/best`).
- **Web Server:** `Flask` (webhook handler) running under `Gunicorn` (1 worker, 8 threads).
- **Deployment:** Render (configured via `Procfile`, `requirements.txt`, and `runtime.txt`).
- **File Structure:** Single-file monolithic script (`main.py`) for clean, self-contained deployment.
- **Admin Commands:** `/stats` for admins (configured via `ADMIN_IDS` env var).

---

## 🔄 Core Workflow & Logic

1. **Input Detection:** Monitor all text messages. Validate domains against known TikTok, Instagram, Facebook, and Twitter/X patterns.
2. **Instagram Edge-case:** Strictly reject URLs containing `/p/` (photos/carousels) early to prevent redundant download triggers.
3. **Queue System (FIFO per user):** Each URL received is validated and then enqueued into a per-user `asyncio.Queue`. A dedicated background worker (`_queue_worker`) processes tasks in order. Workers auto-terminate after 5 minutes of inactivity.
4. **TikTok Slideshows:** Detect `/photo/` URLs. Bypass `yt-dlp` and utilize the `tikwm.com` API as a robust fallback.
5. **Download Phase:** Initialize `yt-dlp`. The output is written to a temp file in `tempfile.gettempdir()`. No progress is shown to the user — only a static ⏳ emoji.
6. **Delivery Phase:** Upload via Telegram (`read_timeout=120`, `write_timeout=120`, `connect_timeout=30`, file limit `< 50 MB`). If the file has no audio stream, it is sent as `send_animation` (GIF) instead of `send_video`. All uploads use `application.bot.send_*()` (not `update.message.reply_*()`) because the download runs in the queue worker context.
7. **Garbage Collection:** Always clean up temporary files, even if the upload fails.
8. **Metrics:** Track total requests, successful downloads, failed downloads, and unique users in a thread-safe global dict (`_stats` with `_stats_lock`).

---

## ⚙️ Hard Rules & Network Tunings

When writing or refactoring code, you must strictly adhere to these parameters:

### yt-dlp Configuration

- `socket_timeout`: `120` seconds.
- `extractor_retries`: `3` attempts.
- `file_access_retries`: `3` attempts.
- `merge_output_format`: Always force `mp4` for standard Telegram client playback compatibility.
- `cookies`: Optionally load from a file path defined in `COOKIES_FILE` environment variable, falling back to `tempfile/cookies.txt`.
- `cachedir`: Always set to `YDL_CACHE_DIR` env var (or `<tempdir>/ydl_cache`). This enables yt-dlp's built-in extractor cache to avoid re-fetching info for repeated URLs.
- `impersonate`: Always set to `""` (empty string). Enables TLS fingerprint impersonation (auto-select client) via `curl_cffi` when available, to bypass Cloudflare and other bot protections on sites like Reddit. Falls back gracefully if `curl_cffi` is not installed.

### Queue System

- **Per-user FIFO queue:** Each user has their own `asyncio.Queue[DownloadTask]`. Workers are created on-demand and auto-terminate after 5 minutes of inactivity.
- **Executor:** A shared `ThreadPoolExecutor(max_workers=2)` handles yt-dlp operations across all queues.
- **Error isolation:** Errors in `_execute_download` are caught by the worker and reported to the user via the processing message, without crashing the worker.

### Metrics & Admin

- **`/stats` command:** Accessible only to user IDs listed in `ADMIN_IDS` env var (comma-separated). Shows uptime, total requests, successes, failures, unique users, and active queues.
- **Global stats dict (`_stats`):** Thread-safe (`_stats_lock`). Tracks `start_time`, `total_requests`, `successful`, `failed`, `unique_users`.
- **Health endpoint (`/health`):** Returns JSON with `status`, `yt_dlp_version`, `bot_ready`, and `queues` info (active queues + workers). Useful for Render monitoring.

### UX & Error Handling

- **State management:** Use a single "⏳" status message. Show "⏳ En cola..." initially, then edit to "⏳" when processing starts. Always **edit** this message to show failure. Never edit it to show download progress. Never send duplicate/spammy error messages.
- **Error translations:** Catch known `yt-dlp` exceptions (e.g., Geo-restriction, private video, deleted content) and map them to friendly, localized Spanish errors instead of throwing raw stack traces to the user.
- **Start message sync:** Whenever a new platform or feature is added (e.g., GIF support, a new domain, queue system), the `/start` command text must be updated to reflect it. This ensures users always see an accurate list of supported platforms and capabilities.

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | ✅ | — | Bot token de Telegram |
| `RENDER_EXTERNAL_HOSTNAME` | ❌ | — | URL externa de Render (para webhook) |
| `ADMIN_IDS` | ❌ | — | IDs de Telegram separados por coma para `/stats` |
| `COOKIES_FILE` | ❌ | `<tempdir>/cookies.txt` | Ruta al archivo de cookies |
| `YDL_CACHE_DIR` | ❌ | `<tempdir>/ydl_cache` | Directorio para caché de extractores de yt-dlp |
| `MAX_URLS_PER_MESSAGE` | ❌ | `20` | Máximo de URLs permitidas por mensaje |
| `PORT` | ❌ | `8080` | Puerto del servidor Flask |

---

## 💻 Coding Standards (For the AI Agent)

- **Async First:** Avoid blocking the event loop. Always wrap synchronous blocking calls (like file system writes or `yt-dlp` invocations) using `loop.run_in_executor()` with `_download_executor`.
- **Conciseness:** Provide direct code updates. When modifying `main.py`, output the specific changed function or block rather than rewriting the entire file, unless explicitly requested.
- **Language & Documentation:** Maintain all user-facing bot messages, code comments, and chat explanations in **Spanish**. All code must include clear comments documenting its purpose and logic in Spanish.
- **Sync AGENTS.md on every change:** Every integration, change, new feature, or flow modification **must** be documented immediately in this `AGENTS.md`. This file must always reflect the exact current state of the project. No functionality change is complete until `AGENTS.md` is updated accordingly.
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
