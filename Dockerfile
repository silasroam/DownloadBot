# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 - builder
# Compiles/collects a wheel for every dependency.  TgCrypto has no prebuilt
# wheel for python 3.12, so a C toolchain is needed here - and only here.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

# ---------------------------------------------------------------------------
# JS runtime - required for YouTube since yt-dlp 2025.11.12
# yt-dlp invokes Deno to solve YouTube's player/nsig challenges; without it
# format availability is limited and downloads increasingly fail.  The bin
# image is scratch-based, only the single static binary is copied out of it.
# ---------------------------------------------------------------------------
FROM denoland/deno:bin-2.9.7 AS deno

# ---------------------------------------------------------------------------
# Stage 2 - runtime
# Minimal image: system ffmpeg (audio extraction / mp4 merge), ca-certificates
# (yt-dlp over TLS), the prebuilt wheels and the bot sources.  Runs as uid 10001.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/app \
    PORT=10000 \
    DOWNLOAD_DIR=/app/downloads

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

# Deno for yt-dlp's YouTube challenge solver (see the "deno" stage above).
# Deno is the only JS runtime yt-dlp enables by default, so being on PATH is enough.
COPY --from=deno /deno /usr/local/bin/deno

# Never run the bot as root (Render free tier friendly).
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /wheels /wheels
COPY requirements.txt ./
RUN pip install --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY --chown=app:app main.py config.py downloader.py webserver.py strings.py keyboards.py database.py pyrocompat.py ./
RUN mkdir -p /app/downloads && chown -R app:app /app

USER app

# Render injects its own PORT value; this is only the local default.
EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ.get("PORT", "10000") + "/health", timeout=5)'

CMD ["python", "main.py"]
