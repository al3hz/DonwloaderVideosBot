# DownloaderVideosBot

Bot de Telegram para descargar videos de **TikTok**, **Instagram Reels**, **Facebook** y **Twitter/X**.

## Cómo usar

Envía un enlace al bot y este te devolverá el video descargado.

**Plataformas soportadas:**
- TikTok (videos y slideshows)
- Instagram (solo Reels)
- Facebook (videos / Reels)
- Twitter / X

**Límite:** 50 MB por archivo (límite de Telegram para bots).

## Stack

- Python 3.11+ asyncio
- python-telegram-bot v22 (webhooks)
- yt-dlp
- Flask + Gunicorn
- Render

## Archivos

| Archivo | Descripción |
|---------|-------------|
| `main.py` | Bot completo |
| `Procfile` | Comando de inicio para Render |
| `requirements.txt` | Dependencias |
| `runtime.txt` | Versión de Python |
| `.opencode/agent/AGENTS.md` | Instrucciones para IA |

## AGENTS.md

Este proyecto incluye un archivo `.opencode/agent/AGENTS.md` con instrucciones detalladas para que una IA (como opencode o Claude) pueda entender, mantener y mejorar el código correctamente. Incluye el stack, flujo de trabajo, reglas de configuración, estándares de código y un protocolo de verificación.

Si usas una IA para trabajar en este proyecto, asígnala a `.opencode/agent/AGENTS.md` primero.
