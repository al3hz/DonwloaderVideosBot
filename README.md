# DownloaderVideosBot

Bot de Telegram para descargar videos de varias plataformas y consultar info de anime/manga.
Sin cookies y sin keys de API. Si un video supera los 50 MB de Telegram, se reintenta en
calidad reducida y, como ultimo recurso, se re-encodea a <=720p con ffmpeg.

## Como usar

Envia uno o varios enlaces al bot y los procesara en orden (cola FIFO por usuario).
Tambien puedes enviar un **screenshot** de anime para que el bot lo identifique.

**Plataformas soportadas (descargas):**

| Plataforma | Contenido |
|---|---|
| TikTok | Videos (sin marca de agua), slideshows, posts de una sola imagen |
| Facebook | Videos / Reels |
| Twitter / X | Videos, GIFs e imagenes de tweets |
| Reddit | Videos (v.redd.it), imagenes, GIFs |
| Bilibili | Anime, clips, AMVs, bangumi |
| Niconico | Anime, MADs, musica |

**Limite:** 50 MB por archivo.

## Funciones de anime

| Comando | Descripcion |
|---|---|
| `/anime <nombre>` | Busca un anime y muestra score, episodios, generos, estado y **sinopsis traducida al espanol** |
| `/manga <nombre>` | Igual que `/anime` pero para manga (capitulos y volumenes) |
| `/temporada` | Animes mas populares de la temporada actual |
| `/hoy` | Emisiones de anime de las proximas 24 horas |
| `/waifu` | Imagen random de waifu (nekos.best) |
| Enviar un screenshot | Identifica el anime, episodio y timestamp (trace.moe) |

## Comandos

| Comando | Descripcion |
|---|---|
| `/start` | Mensaje de bienvenida |
| `/help` | Alias de `/start` |
| `/id` | Muestra tu `user_id` y `chat_id` (util para configurar `ADMIN_IDS`) |
| `/queue` | Muestra tus descargas pendientes en cola |
| `/cancel` | Cancela tus descargas pendientes |
| `/config` | Configura si ver el credito del bot y el titulo del video (por usuario) |
| `/stats` | Estadisticas del bot, por plataforma (solo admins) |
| `/anime` | Buscar info de un anime |
| `/manga` | Buscar info de un manga |
| `/temporada` | Animes de la temporada actual |
| `/hoy` | Emisiones de las proximas 24h |
| `/waifu` | Imagen random de waifu |

## Variables de entorno

| Variable | Requerida | Descripcion |
|---|---|---|
| `TELEGRAM_TOKEN` | Si | Token del bot de Telegram |
| `TELEGRAM_WEBHOOK_SECRET` | No | Secreto para validar el webhook (header `X-Telegram-Bot-Api-Secret-Token`). Recomendado en produccion |
| `ADMIN_IDS` | No | IDs de Telegram (separados por coma) para acceder a `/stats` |
| `PORT` | No | Puerto del servidor (default: 8080) |
| `RENDER_EXTERNAL_HOSTNAME` | No | Hostname en Render para configurar el webhook |
| `COOKIES_FILE` | No | Ruta a archivo de cookies en formato Netscape |
| `OUTBOUND_PROXY_URL` | No | Proxy HTTP/SOCKS5 para TikTok/tikwm/yt-dlp cuando la IP del host (ej. Render) esta bloqueada por Cloudflare |
| `YDL_CACHE_DIR` | No | Directorio de cache para yt-dlp |
| `MAX_URLS_PER_MESSAGE` | No | Maximo de URLs por mensaje (default: 20) |
| `SPOTDL_MAX_TRACKS` | No | Tope de pistas por lista de Spotify (default: 15) |
| `YOUTUBE_COOKIES_B64` | No | Cookies de YouTube en base64 — **necesarias** para descargar musica desde IPs de datacenter (Render), sino YouTube pide "Sign in to confirm you're not a bot" |
| `UPSTASH_REDIS_REST_URL` | No | URL REST de Upstash Redis — persiste `/config` de cada usuario y las stats del bot entre reinicios |
| `UPSTASH_REDIS_REST_TOKEN` | No | Token REST correspondiente |

### Persistencia gratis (Upstash Redis)

Sin BD, la configuracion y stats se pierden en cada reinicio/spin-down de Render.
Para persistirlas gratis (plan Free de Upstash, ~10k comandos/dia, suficiente):

1. Crea cuenta en `upstash.com` → **Create Database** → Regional → elige la region mas
   cercana a tu Render (ej. `us-east-1`) → **REST API**
2. Copia los valores `UPSTASH_REDIS_REST_URL` y `UPSTASH_REDIS_REST_TOKEN`
3. En Render: **Environment** → agrega ambas variables → deploy

No requiere dependencias nuevas (usa `requests` contra la API REST) ni conexiones
persistentes (compatible con spin-downs). Sin las variables, el bot funciona igual
pero todo vive solo en memoria.

### Cookies de YouTube (requerido para la musica en Render)

YouTube bloquea las descargas desde IPs de datacenter con *"Sign in to confirm
you're not a bot"*. La solucion es pasarle cookies de un navegador:

1. Abre `youtube.com` en tu navegador (no hace falta iniciar sesion; con sesion
   iniciada suele ser mas estable, pero usa una cuenta secundaria por prudencia)
2. Instala la extension **Get cookies.txt LOCALLY** (Chrome/Firefox) y exporta
   las cookies de `youtube.com` a un archivo `cookies.txt`
3. Codificalo a base64 y copialo al portapapeles:
   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("cookies.txt")) | Set-Clipboard
   ```
4. En Render: **Environment** → crea `YOUTUBE_COOKIES_B64` y pega el valor → deploy

Las cookies expiran cada tanto (semanas/meses): si vuelve el error, re-exporta y
actualiza la variable. Nunca subas el `cookies.txt` al repo.

## APIs externas

| API | Uso |
|---|---|
| yt-dlp | Descargas de todas las plataformas |
| tikwm.com | Fallback para TikTok: slideshows y posts `/photo/` (yt-dlp no soporta fotos) y videos sensibles |
| fxtwitter / vxtwitter | Fallback para imagenes de tweets |
| AniList (GraphQL) | Info de anime/manga, temporada y emisiones |
| nekos.best | Imagenes de waifu |
| trace.moe | Identificacion de anime por screenshot |
| Google Translate | Traduccion de sinopsis al espanol |

> Nota: yt-dlp NO soporta posts `/photo/` de TikTok ni extrae las imagenes de los
> slideshows (solo su audio, mp3/m4a). Esos casos dependen del fallback tikwm.com,
> al que se accede con `curl_cffi` impersonando Chrome porque Cloudflare bloquea
> fingerprints TLS no-navegador tipicos de IPs de datacenter (Render).

## Stack

- Python 3.11+
- python-telegram-bot v21+ (webhooks)
- yt-dlp (con fallbacks por plataforma)
- Flask + Gunicorn
- Render

## Despliegue en Render

**Build Command** (en el dashboard o en `render.yaml`):

```
pip install -r requirements.txt && pip install -U --force-reinstall https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz && mkdir -p bin && curl -fsSL -o /tmp/deno.zip https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip && python -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('bin')" && chmod +x bin/deno
```

> El paso extra instala **deno** (runtime JS) en `./bin/`: yt-dlp lo necesita para
> descargar de YouTube sin 403 (anti-bot). Sin deno, la funcion de Spotify no funciona.

**Start Command** (auto: `Procfile`): `gunicorn main:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300 --keep-alive 5`

**Health Check Path:** `/health` (siempre responde 200 mientras el proceso viva; `bot_ready` indica si el bot esta listo).

**Variables extra:** `TELEGRAM_WEBHOOK_SECRET` (valor secreto para proteger el webhook).

> Nota: el yt-dlp nightly/master se instala en el **Build**, no en el start. Asi no retrasa
> el readiness en cada deploy/restart/spin-up y el free tier no quema horas innecesariamente.

## Archivos

| Archivo | Descripcion |
|---|---|
| `main.py` | Bot completo |
| `Procfile` | Comando de inicio para Render |
| `requirements.txt` | Dependencias |
| `runtime.txt` | Version de Python |
| `env_example.env` | Plantilla de variables de entorno |
