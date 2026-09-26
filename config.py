"""Configuration for the Telegram media downloader bot.

Every value is read from the environment (optionally from a ``.env`` file that
lives next to this module).  Import the module and use its attributes; call
:func:`validate` once at startup to fail fast when something is missing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from dotenv import load_dotenv

from strings import LogMessages

log = logging.getLogger(__name__)

BASE_DIR: Path = Path(__file__).resolve().parent

# Loads BASE_DIR/.env if it exists; real environment variables always win.
load_dotenv(BASE_DIR / ".env")

DEFAULT_SESSION_NAME = "downloader_bot"
DEFAULT_DOWNLOAD_DIR = "downloads"
DEFAULT_MAX_FILE_SIZE_MB = 2000
#: Port Render assigns to web services when PORT is not set explicitly.
DEFAULT_PORT = 10000
#: Self-ping cadence default (Render free tier sleeps after ~15 min idle).
DEFAULT_SELF_PING_INTERVAL_MINUTES = 13


class ConfigError(RuntimeError):
    """Raised when the environment configuration is missing or invalid."""


def _get_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"Environment variable {name} must be an integer, got {raw!r}.") from exc


def _get_port() -> int:
    """Read ``PORT``, falling back to :data:`DEFAULT_PORT` if the value is unusable.

    Render always injects a valid port; a malformed value must not stop the
    service, so the default is used and the problem is logged instead.
    """
    raw = _get_str("PORT")
    if not raw:
        return DEFAULT_PORT

    try:
        port = int(raw)
    except ValueError:
        port = 0

    if 1 <= port <= 65535:
        return port

    log.warning(LogMessages.INVALID_PORT.format(value=raw, default_port=DEFAULT_PORT))
    return DEFAULT_PORT


# --- Telegram credentials -----------------------------------------------------
API_ID: int = _get_int("API_ID", 0)
API_HASH: str = _get_str("API_HASH")
BOT_TOKEN: str = _get_str("BOT_TOKEN")
SESSION_NAME: str = _get_str("SESSION_NAME", DEFAULT_SESSION_NAME) or DEFAULT_SESSION_NAME

# --- Download limits ----------------------------------------------------------
# Telegram allows bots to upload up to 2000 MiB per file (4000 MiB for premium
# accounts).  Pyrogram enforces the same limit internally, we check it as well
# to produce a friendly error message before wasting bandwidth.
MAX_FILE_SIZE_MB: int = _get_int("MAX_FILE_SIZE_MB", DEFAULT_MAX_FILE_SIZE_MB)
MAX_FILE_SIZE: int = MAX_FILE_SIZE_MB * 1024 * 1024

# --- Storage ------------------------------------------------------------------
_download_dir = Path(_get_str("DOWNLOAD_DIR", DEFAULT_DOWNLOAD_DIR) or DEFAULT_DOWNLOAD_DIR)
DOWNLOAD_DIR: Path = _download_dir if _download_dir.is_absolute() else BASE_DIR / _download_dir

# --- Optional tuning ----------------------------------------------------------
WORKERS: int = _get_int("WORKERS", 4)
MAX_CONCURRENT_TRANSMISSIONS: int = _get_int("MAX_CONCURRENT_TRANSMISSIONS", 4)
HTTP_PROXY: str = _get_str("HTTP_PROXY")
FFMPEG_PATH: str = _get_str("FFMPEG_PATH")

# --- Web service (Render.com) -------------------------------------------------
# Render assigns the public port through PORT (10000 by default) and kills
# containers whose port never answers, so the health server binds to it.
HOST: str = _get_str("HOST", "0.0.0.0") or "0.0.0.0"
PORT: int = _get_port()

# Injected automatically by Render.  When set, the bot pings its own health
# endpoint to keep a free-tier instance from being spun down after ~15 min.
RENDER_EXTERNAL_URL: str = _get_str("RENDER_EXTERNAL_URL")

# Ping cadence in minutes.  Clamped to 1..14 minutes: Render's free tier sleeps
# the service after ~15 minutes without inbound traffic, so a longer interval
# would never prevent the spin-down.
SELF_PING_INTERVAL_MINUTES: int = _get_int("SELF_PING_INTERVAL_MINUTES", DEFAULT_SELF_PING_INTERVAL_MINUTES)
SELF_PING_INTERVAL_SECONDS: float = float(min(max(SELF_PING_INTERVAL_MINUTES, 1), 14) * 60)


def validate() -> None:
    """Raise :class:`ConfigError` with a readable report if anything is missing."""
    problems: list[str] = []

    if API_ID <= 0:
        problems.append("API_ID is missing or is not a positive integer")
    if not API_HASH:
        problems.append("API_HASH is missing")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN is missing")
    if MAX_FILE_SIZE_MB <= 0:
        problems.append("MAX_FILE_SIZE_MB must be a positive integer")

    if problems:
        details = "\n".join(f"  - {problem}" for problem in problems)
        raise ConfigError(
            f"Invalid configuration:\n{details}\n\n"
            f"Copy {BASE_DIR / '.env.example'} to {BASE_DIR / '.env'} and fill it in."
        )


def ensure_download_dir() -> Path:
    """Create the temporary download directory if needed and return it."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR


def get_proxy() -> dict | None:
    """Parse ``HTTP_PROXY`` into the dict format expected by Pyrogram.

    Accepts ``http://user:pass@host:port`` and ``socks5://host:port`` style URLs.
    Returns ``None`` when no proxy is configured.
    """
    if not HTTP_PROXY:
        return None

    parsed = urlsplit(HTTP_PROXY if "://" in HTTP_PROXY else f"http://{HTTP_PROXY}")
    if not parsed.hostname:
        return None

    proxy: dict[str, object] = {
        "scheme": (parsed.scheme or "http").lower(),
        "hostname": parsed.hostname,
        "port": parsed.port or 8080,
    }
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)

    return proxy
