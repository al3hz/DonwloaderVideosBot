# DownloaderVideosBot

Bot de Telegram para descargar videos de varias plataformas y consultar info de anime/manga.
Sin ffmpeg, sin cookies y sin keys de API.

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
| `YDL_CACHE_DIR` | No | Directorio de cache para yt-dlp |
| `MAX_URLS_PER_MESSAGE` | No | Maximo de URLs por mensaje (default: 20) |

## APIs externas

| API | Uso |
|---|---|
| yt-dlp | Descargas de todas las plataformas |
| tikwm.com | Fallback para TikTok (slideshows y videos sensibles) |
| fxtwitter / vxtwitter | Fallback para imagenes de tweets |
| AniList (GraphQL) | Info de anime/manga, temporada y emisiones |
| nekos.best | Imagenes de waifu |
| trace.moe | Identificacion de anime por screenshot |
| Google Translate | Traduccion de sinopsis al espanol |

## Stack

- Python 3.11+
- python-telegram-bot v21+ (webhooks)
- yt-dlp (con fallbacks por plataforma)
- Flask + Gunicorn
- Render

## Despliegue en Render

**Build Command** (en el dashboard o en `render.yaml`):

```
pip install -r requirements.txt && pip install -U --force-reinstall https://github.com/yt-dlp/yt-dlp/archive/master.tar.gz
```

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
