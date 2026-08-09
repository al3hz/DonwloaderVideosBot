# DownloaderVideosBot

Bot de Telegram para descargar videos de **TikTok**, **Instagram Reels**, **Facebook**, **Twitter/X** y **Reddit**.

## Como usar

Envia uno o varios enlaces al bot y los procesara en orden (cola FIFO por usuario).

**Plataformas soportadas:**

| Plataforma | Contenido |
|---|---|
| TikTok | Videos (sin marca de agua), slideshows, posts de una sola imagen |
| Instagram | Reels |
| Facebook | Videos / Reels |
| Twitter / X | Videos, GIFs |
| Reddit | Videos (v.redd.it), imagenes, GIFs |

**Limite:** 50 MB por archivo.

**Comandos:**

| Comando | Descripcion |
|---|---|
| `/start` | Mensaje de bienvenida |
| `/stats` | Estadisticas del bot (solo admins) |

## Variables de entorno

| Variable | Requerida | Descripcion |
|---|---|---|
| `TELEGRAM_TOKEN` | Si | Token del bot de Telegram |
| `ADMIN_IDS` | No | IDs de Telegram (separados por coma) para acceder a `/stats` |
| `PORT` | No | Puerto del servidor (default: 8080) |
| `RENDER_EXTERNAL_HOSTNAME` | No | Hostname en Render para configurar el webhook |
| `COOKIES_FILE` | No | Ruta a archivo de cookies en formato Netscape |
| `YDL_CACHE_DIR` | No | Directorio de cache para yt-dlp |
| `MAX_URLS_PER_MESSAGE` | No | Maximo de URLs por mensaje (default: 20) |

## Stack

- Python 3.11+
- python-telegram-bot v21+ (webhooks)
- yt-dlp (con fallback a tikwm.com para TikTok)
- Flask + Gunicorn
- Render

## Archivos

| Archivo | Descripcion |
|---|---|
| `main.py` | Bot completo |
| `Procfile` | Comando de inicio para Render |
| `requirements.txt` | Dependencias |
| `runtime.txt` | Version de Python |
| `env_example.env` | Plantilla de variables de entorno |
