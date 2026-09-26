# TG Media Downloader Bot

Telegram bot built with **Pyrogram** + **yt-dlp** that downloads video/audio from
1000+ sites, uploads it back to the chat (up to 2 GB per file) and deletes every
temporary file afterwards.

It is packaged for **Render.com** (Web Service, free tier): the process runs a
small HTTP health server on `PORT` (Render kills containers that never answer)
and, when `RENDER_EXTERNAL_URL` is available, a background task pings its own
`/health` endpoint every 13 minutes so the free instance is not spun down.

## Features

- `/start` / `/help` greeting, then send any link → inline keyboard **🎬 Видео** / **🎵 Аудио**
- video: `bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best` merged into MP4
- audio: best stream extracted to **MP3** by the system `ffmpeg`
- live progress in the same message (download % / speed / ETA and upload %)
- non-blocking: every blocking yt-dlp call runs in `run_in_executor()`
- 2 GB cap enforced before *and* after downloading, `supports_streaming=True` for videos
- guaranteed cleanup: per-job temp folder is deleted in a `finally` block on success and on error
- health endpoints `GET /`, `GET /health`, `GET /healthz` → `{"status": "ok", ...}`

## Requirements

- Docker (recommended) **or** Python 3.10+ with `ffmpeg` installed
- [Deno](https://deno.com/) 2.0+ on `PATH`: yt-dlp uses it as the JavaScript
  runtime for YouTube since version 2025.11.12 (the Docker image already ships
  it; for local runs install it yourself, e.g. `curl -fsSL https://deno.land/install.sh | sh`)
- Telegram `API_ID` / `API_HASH` (<https://my.telegram.org/apps>) and a bot token
  from [@BotFather](https://t.me/BotFather)

## Local run

```bash
git clone <your-repo-url> tg-media-downloader && cd tg-media-downloader
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # fill API_ID, API_HASH, BOT_TOKEN
python main.py              # health server on http://127.0.0.1:10000
curl http://127.0.0.1:10000/health
```

Or with Docker (same as Render):

```bash
docker build -t tg-media-downloader .
docker run --rm -p 10000:10000 --env-file .env tg-media-downloader
```

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `API_ID` | – | **required**, Telegram API id |
| `API_HASH` | – | **required**, Telegram API hash |
| `BOT_TOKEN` | – | **required**, bot token from BotFather |
| `SESSION_NAME` | `downloader_bot` | Pyrogram session file name (created next to `main.py`) |
| `MAX_FILE_SIZE_MB` | `2000` | hard upload cap (Telegram limit for bots) |
| `DOWNLOAD_DIR` | `downloads/` | temp storage; wiped after every job |
| `PORT` | `10000` | port for the health server (Render injects it; invalid values fall back to the default and are logged) |
| `HOST` | `0.0.0.0` | bind address of the health server |
| `RENDER_EXTERNAL_URL` | – | injected by Render; enables the self-ping keep-alive |
| `SELF_PING_INTERVAL_MINUTES` | `13` | ping cadence, clamped to 1–14 minutes |
| `FFMPEG_PATH` | – | explicit ffmpeg binary (e.g. `/usr/bin/ffmpeg`) |
| `HTTP_PROXY` | – | `http://user:pass@ip:port` or `socks5://ip:port` |
| `WORKERS`, `MAX_CONCURRENT_TRANSMISSIONS` | `4` | Pyrogram tuning |

`FFMPEG_PATH` is optional: ffmpeg is on `PATH` in the Docker image and in a normal
Arch/Debian install, and yt-dlp finds it by itself.

## Deploy on Render.com

### 1. Push the project to GitHub

```bash
cd tg-media-downloader
git init
git add .
git commit -m "Telegram media downloader bot"
git branch -M main
git remote add origin git@github.com:<your-user>/<your-repo>.git
git push -u origin main
```

`.gitignore` keeps `.env`, `*.session` and `downloads/` out of the repository —
never commit your credentials.

### 2. Option A — Blueprint (`render.yaml`, recommended)

1. Render Dashboard → **New** → **Blueprint**.
2. Pick the repository (Render reads `render.yaml` from the root).
3. Fill in `API_ID`, `API_HASH`, `BOT_TOKEN` when prompted (`sync: false` values
   are asked at creation time and stored encrypted).
4. **Apply** and wait for the first build. Render builds `./Dockerfile`,
   injects `PORT` and `RENDER_EXTERNAL_URL`, and uses `/health` for health checks.

### 3. Option B — manual Web Service

1. Dashboard → **New** → **Web Service** → connect the repository.
2. Language/Runtime: **Docker**; Instance type: **Free**; Dockerfile path `./Dockerfile`.
3. Health check path: `/health`.
4. Environment → add `API_ID`, `API_HASH`, `BOT_TOKEN` (and optionally
   `MAX_FILE_SIZE_MB`, `DOWNLOAD_DIR=/app/downloads`).
   Do **not** set `PORT` — Render injects it and warns that it is reserved.
5. Create the service. The first build takes a few minutes (compiles TgCrypto,
   installs ffmpeg).

### 4. Verify

- `https://<your-service>.onrender.com/health` → `{"status":"ok","uptime_seconds":…}`
- Logs should show, in this order:

```
📁 Каталог временных загрузок: /app/downloads
🌐 Модуль проверки состояния запущен на http://0.0.0.0:10000
✅ Сервис @<your_bot> запущен (MTProto). Лимит: 2000 MB. Для остановки нажмите Ctrl+C
♻️ Самопинг включен: https://<your-service>.onrender.com/health каждые 780 с
```

Then send `/start` to the bot in Telegram and try a link.

## Free tier notes

- **Spin-down:** free web services sleep after ~15 minutes without inbound HTTP
  traffic. The self-ping loop (`SELF_PING_INTERVAL_MINUTES=13`) creates inbound
  traffic so the bot stays online; if you do not want that, remove
  `RENDER_EXTERNAL_URL` from the service and the loop is disabled automatically.
- **Ephemeral disk:** the container filesystem is wiped on every deploy/restart,
  so the Pyrogram `*.session` file is recreated (a new auth-key exchange, a few
  seconds) and any in-flight download is lost.
- **512 MB RAM / limited disk:** a huge merged MP4 can be OOM-killed — lower
  `MAX_FILE_SIZE_MB` (e.g. `1000`) in the service environment if that happens.
- Free instances are subject to Render's monthly instance-hours limit; the
  keep-alive above means the service uses hours continuously.
- Deployment restarts send `SIGTERM`; the bot handles it by stopping the
  self-ping task, releasing the port and closing the Pyrogram session cleanly.

## Project layout

```
main.py         Pyrogram client, handlers, job pipeline, startup/shutdown
webserver.py    aiohttp health server (PORT) + self-ping keep-alive
downloader.py   async yt-dlp wrapper (executor, progress, size caps, errors)
config.py       .env / environment parsing + validation
strings.py      UI texts (UI.*) and log lines (LogMessages.*) — single place to edit wording
Dockerfile      multi-stage image with ffmpeg, non-root user
render.yaml     Render Blueprint (Docker web service, free plan)
requirements.txt
```

## Localization

Every user-facing text and log line lives in `strings.py`:

- `UI.*` — messages, buttons, alerts and errors sent to Telegram (`UI.progress()`,
  `UI.caption()`, `UI.completed()`, `UI.ERR_*`, …);
- `LogMessages.*` — console/Render logs (`LogMessages.BOT_STARTED`, `LINK_FAIL`,
  `HEALTH_RUNNING`, …); placeholders are filled with `str.format()` at the call site.

Parse modes follow the markup in that file: the `/start` greeting is the only
message sent with `ParseMode.MARKDOWN` (it contains `**bold**`), everything else —
progress lines, captions, errors — is sent with `ParseMode.DISABLED`, so video
titles and URLs containing `*`, `_` or `<` can never break Telegram entities.

Strings for the planned media-upload/conversion flow (`BUTTON_MP3`, `BUTTON_OGG`,
`converting()`, `ERR_CONVERT`, the `DB_*` and `SESSION_*` log lines) are already in
place but intentionally unused until those handlers are implemented.

## Notes

- Pyrogram 2.0.x calls `asyncio.get_event_loop()` at import time, which raises on
  Python 3.12+ unless a loop exists; `main.py` installs a throw-away loop before
  importing Pyrogram and builds the client *inside* the running loop. This keeps
  Python 3.10–3.14 working unchanged.
- The bot only serves private chats; every download is executed under a per-user
  lock so one user cannot queue parallel jobs.

