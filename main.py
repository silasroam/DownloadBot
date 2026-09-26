"""Telegram media downloader bot (Pyrogram + yt-dlp), ready for Render.com.

Run it with::

    python main.py

Flow: /start -> send a URL -> pick a category (muted video / audio / video
with sound) -> pick the quality or audio format -> the file is downloaded
into a temporary folder, uploaded to Telegram and deleted right after.

Deployment: alongside the bot the process starts a tiny aiohttp server on
``PORT`` (Render requires an HTTP listener on the assigned port) and, when
``RENDER_EXTERNAL_URL`` is present, a background task that pings that URL so a
free-tier instance is not spun down after ~15 minutes of inactivity.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

# --- Python 3.12+ compatibility shim -----------------------------------------
# Pyrogram 2.0.x runs ``asyncio.get_event_loop()`` while importing
# ``pyrogram/sync.py``.  Since Python 3.12 that call no longer creates a loop
# implicitly (and raises RuntimeError on 3.14), which breaks ``import pyrogram``
# completely.  ``pyrocompat`` installs a placeholder loop before the import;
# the real loop is created later by ``asyncio.run()`` in :func:`main`.
import pyrocompat  # noqa: E402  (must run before the pyrogram import below)
# -----------------------------------------------------------------------------

from pyrogram import Client, filters, idle
from pyrogram.enums import ParseMode
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
)

import config
import database
from downloader import (
    DEFAULT_AUDIO_FORMAT,
    DEFAULT_HEIGHT,
    MODE_AUDIO as DL_MODE_AUDIO,
    DownloadFailed,
    DownloadResult,
    DownloaderError,
    MediaTooLargeError,
    download_media,
    human_size,
    human_time,
    is_valid_audio_format,
    is_valid_height,
)
from keyboards import (
    BACK_PATTERN,
    CATEGORY_LABELS,
    CATEGORY_PATTERN,
    CATEGORY_TITLES,
    SELECTION_PATTERN,
    category_keyboard,
    quality_keyboard,
)
from strings import LogMessages, UI
from webserver import self_ping_loop, start_webserver, stop_webserver


log = logging.getLogger(__name__)

#: Matches http(s) links inside a message.
URL_REGEX = re.compile(r"https?://[^\s<>\"'«»]+", re.IGNORECASE)
#: Punctuation that usually follows a link in a sentence instead of belonging to it.
URL_TRAILING_CHARS = ".,;:!?)]}\"«»'"
#: Callback payloads are kept short to stay inside Telegram's 64 byte limit.
#: ``keyboards.py`` owns the three patterns (``cat:``/``sel:``/``back:``) and
#: the keyboard builders; the handlers below only dispatch them.

#: Minimum seconds between two progress edits of the same message.
PROGRESS_EDIT_INTERVAL = 3.0
UPLOAD_EDIT_INTERVAL = 3.0
#: Maximum number of remembered "pending" keyboards.
PENDING_LIMIT = 500

#: The greeting uses ``**bold**`` markup, so it is parsed as Markdown.
MARKUP_PARSE_MODE = ParseMode.MARKDOWN
#: Anything that may embed user data (URLs, titles, yt-dlp errors) is sent
#: literally - otherwise Telegram rejects the message while parsing entities.
TEXT_PARSE_MODE = ParseMode.DISABLED

#: Raised internally when Pyrogram refuses a file above the upload limit.
class _SendTooLargeError(Exception):
    """Telegram refused the file because it is bigger than the allowed 2000 MiB."""

# key -> {"url", "user_id", "chat_id", "message_id", "created_at"}
_pending_downloads: dict[str, dict[str, Any]] = {}
# users that currently have a download in flight
_active_users: set[int] = set()
# (chat_id, message_id) -> monotonic timestamp of the last edit
_last_edit_at: dict[tuple[int, int], float] = {}


# ---------------------------------------------------------------------------
# Small UI helpers
# ---------------------------------------------------------------------------
async def _safe_answer(
    query: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    """Answer a callback query, ignoring expired / duplicated clicks."""
    try:
        await query.answer(text=text, show_alert=show_alert)
    except FloodWait as exc:
        log.warning("FloodWait for %s s while answering a callback", exc.value)
    except Exception as exc:  # expired query id, network hiccup, ...
        log.debug("Callback answer failed: %s", exc)


async def _safe_edit(
    client: Client,
    chat_id: int,
    message_id: int,
    text: str,
    progress_key: Optional[tuple[int, int]] = None,
) -> bool:
    """Edit a status message, swallowing "not modified" / flood errors.

    Returns ``True`` when the edit was actually sent to Telegram.
    """
    if not chat_id or not message_id:
        return False

    if progress_key is not None:
        now = time.monotonic()
        last = _last_edit_at.get(progress_key, 0.0)
        if now - last < PROGRESS_EDIT_INTERVAL:
            return False
        _last_edit_at[progress_key] = now

    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text[:4096],
            parse_mode=TEXT_PARSE_MODE,
            disable_web_page_preview=True,
        )
        return True
    except MessageNotModified:
        return False
    except FloodWait as exc:
        wait_for = min(exc.value + 1, 30)
        log.warning("FloodWait: sleeping %s s before editing message %s", wait_for, message_id)
        await asyncio.sleep(wait_for)
        with contextlib.suppress(Exception):
            await client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:4096],
                parse_mode=TEXT_PARSE_MODE,
                disable_web_page_preview=True,
            )
        return True
    except Exception as exc:
        log.debug("Could not edit message %s: %s", message_id, exc)
        return False


async def _remove_inline_keyboard(client: Client, chat_id: int, message_id: int) -> None:
    """Detach the format buttons so they cannot be clicked twice."""
    try:
        await client.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
    except Exception as exc:
        log.debug("Could not remove reply markup from %s: %s", message_id, exc)


async def _safe_edit_text_with_markup(
    client: Client,
    chat_id: int,
    message_id: int,
    text: str,
    markup: InlineKeyboardMarkup,
) -> bool:
    """Edit a menu message and replace its inline keyboard (step 1 <-> step 2)."""
    if not chat_id or not message_id:
        return False
    try:
        await client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text[:4096],
            parse_mode=TEXT_PARSE_MODE,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
        return True
    except MessageNotModified:
        return False
    except FloodWait as exc:
        log.warning("FloodWait: %s s while switching the download menu", exc.value)
        await asyncio.sleep(min(exc.value + 1, 30))
        with contextlib.suppress(Exception):
            await client.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text[:4096],
                parse_mode=TEXT_PARSE_MODE,
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        return True
    except Exception as exc:
        log.debug("Could not switch the keyboard of %s: %s", message_id, exc)
        return False


def _job_owner_id(job: dict[str, Any]) -> int:
    """Telegram id that owns a pending job (``0`` when unknown)."""
    return int(job.get("user_id") or 0)


def _query_user_id(query: CallbackQuery) -> int:
    return query.from_user.id if query.from_user else 0


def _forget_progress_key(key: tuple[int, int]) -> None:
    _last_edit_at.pop(key, None)


def _normalize_url(raw: str) -> str:
    return raw.strip().rstrip(URL_TRAILING_CHARS)


def _remember_pending(key: str, payload: dict[str, Any]) -> None:
    """Store the URL behind a button, dropping the oldest entries when full."""
    if len(_pending_downloads) >= PENDING_LIMIT:
        oldest = sorted(_pending_downloads, key=lambda item: _pending_downloads[item]["created_at"])
        for stale in oldest[: PENDING_LIMIT // 2]:
            _pending_downloads.pop(stale, None)
    _pending_downloads[key] = payload


def _build_caption(result: DownloadResult, kind: str, title: Optional[str] = None) -> str:
    """Build the media caption through the localization layer."""
    duration = human_time(result.duration) if result.duration else ""
    return UI.caption(kind, title or result.title, duration, human_size(result.filesize))


def _make_upload_progress(client: Client, chat_id: int, message_id: int):
    """Create a throttled, coroutine based upload progress callback."""
    state = {"edited_at": 0.0, "last_percent": -1.0, "last_bytes": 0, "last_time": 0.0}

    async def progress(current: int, total: int, *args: Any) -> None:
        now = time.monotonic()
        finished = bool(total) and current >= total
        percent = (current / total * 100.0) if total else 0.0

        # Transfer rate between two callbacks (0 until it can be measured).
        elapsed = now - state["last_time"] if state["last_time"] else 0.0
        if elapsed > 0 and current >= state["last_bytes"]:
            rate = (current - state["last_bytes"]) / elapsed
        else:
            rate = 0.0
        state["last_bytes"], state["last_time"] = current, now

        if not finished:
            if now - state["edited_at"] < UPLOAD_EDIT_INTERVAL:
                return
            if percent - state["last_percent"] < 1.0:
                return
        state["edited_at"] = now
        state["last_percent"] = percent

        left = int((total - current) / rate) if rate > 0 else 0
        text = UI.progress(
            label=UI.SENDING,
            percent=int(percent),
            current=human_size(current),
            total=human_size(total),
            speed=human_size(rate),
            left=left,
        )
        await _safe_edit(
            client,
            chat_id,
            message_id,
            text,
            # Throttle intermediate updates, but always show the final 100%.
            progress_key=None if finished else (chat_id, message_id),
        )

    return progress


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(client: Client, message: Message) -> None:
    """Greet the user with the service description from strings.py."""
    await message.reply_text(
        UI.START,
        parse_mode=MARKUP_PARSE_MODE,
        disable_web_page_preview=True,
    )


async def handle_url(client: Client, message: Message) -> None:
    """Extract the first URL from a text message and offer the category buttons."""
    text = message.text or ""
    match = URL_REGEX.search(text)

    if match is None:
        await message.reply_text(
            UI.NO_LINK,
            parse_mode=TEXT_PARSE_MODE,
            disable_web_page_preview=True,
        )
        return

    url = _normalize_url(match.group(0))
    key = secrets.token_urlsafe(8)

    sent = await message.reply_text(
        UI.choose_category(url),
        parse_mode=TEXT_PARSE_MODE,
        reply_markup=category_keyboard(key),
        disable_web_page_preview=True,
    )

    _remember_pending(
        key,
        {
            "url": url,
            "user_id": message.from_user.id if message.from_user else 0,
            "chat_id": sent.chat.id,
            "message_id": sent.id,
            "created_at": time.time(),
        },
    )


async def _load_job(query: CallbackQuery, key: str, *, pop: bool) -> Optional[dict[str, Any]]:
    """Return the pending job behind ``key`` or raise an "expired" alert.

    Only the user who sent the link may press its buttons.  With ``pop=True``
    the job is consumed (final selection) and restored when it cannot be used.
    """
    job = _pending_downloads.pop(key, None) if pop else _pending_downloads.get(key)
    if job is None:
        await _safe_answer(query, UI.BUTTON_EXPIRED, show_alert=True)
        return None

    if _query_user_id(query) != _job_owner_id(job):
        if pop:
            _pending_downloads[key] = job
        await _safe_answer(query, UI.BUTTON_EXPIRED, show_alert=True)
        return None

    return job


async def handle_category_choice(client: Client, query: CallbackQuery) -> None:
    """Step 1 -> step 2: show the quality/format buttons of the chosen category."""
    matches = query.matches or []
    if not matches:
        await _safe_answer(query, UI.UNKNOWN_BUTTON, show_alert=True)
        return

    category = matches[0].group("category")
    key = matches[0].group("key")
    job = await _load_job(query, key, pop=False)
    if job is None:
        return

    job["category"] = category
    await _safe_answer(query)
    await _safe_edit_text_with_markup(
        client,
        int(job["chat_id"]),
        int(job["message_id"]),
        UI.choose_quality(CATEGORY_TITLES.get(category, category)),
        quality_keyboard(category, key),
    )


async def handle_back(client: Client, query: CallbackQuery) -> None:
    """Step 2 -> step 1: bring the category buttons back."""
    matches = query.matches or []
    if not matches:
        await _safe_answer(query, UI.UNKNOWN_BUTTON, show_alert=True)
        return

    key = matches[0].group("key")
    job = await _load_job(query, key, pop=False)
    if job is None:
        return

    await _safe_answer(query)
    await _safe_edit_text_with_markup(
        client,
        int(job["chat_id"]),
        int(job["message_id"]),
        UI.choose_category(str(job["url"])),
        category_keyboard(key),
    )


async def handle_quality_choice(client: Client, query: CallbackQuery) -> None:
    """Step 2: download the URL with the selected quality / audio format."""
    matches = query.matches or []
    if not matches:
        await _safe_answer(query, UI.UNKNOWN_BUTTON, show_alert=True)
        return

    category = matches[0].group("category")
    value = matches[0].group("value")
    key = matches[0].group("key")
    job = await _load_job(query, key, pop=True)
    if job is None:
        return

    user_id = _query_user_id(query)
    if user_id in _active_users:
        _pending_downloads[key] = job
        await _safe_answer(query, UI.ALERT_BUSY, show_alert=True)
        return

    await _safe_answer(query)
    _active_users.add(user_id)
    try:
        await _process(client, job, category, value)
    finally:
        _active_users.discard(user_id)


# ---------------------------------------------------------------------------
# Download -> upload -> cleanup pipeline
# ---------------------------------------------------------------------------
async def _process(client: Client, job: dict[str, Any], mode: str, value: str = "") -> None:
    """Run one download job and keep the user informed through the same message."""
    chat_id = int(job["chat_id"])
    message_id = int(job["message_id"])
    url = str(job["url"])
    user_id = _job_owner_id(job)
    progress_key = (chat_id, message_id)

    # ``value`` is a height ("720") for the two video categories and an audio
    # codec ("mp3") for the audio category; fall back to the defaults when a
    # stale/foreign value arrives.
    if mode == DL_MODE_AUDIO:
        height = DEFAULT_HEIGHT
        audio_format = value if is_valid_audio_format(value) else DEFAULT_AUDIO_FORMAT
    else:
        height = int(value) if is_valid_height(value) else DEFAULT_HEIGHT
        audio_format = DEFAULT_AUDIO_FORMAT

    # Every job gets its own folder, so cleanup is a single recursive delete.
    config.ensure_download_dir()
    job_dir = Path(tempfile.mkdtemp(prefix="tgdl_", dir=str(config.DOWNLOAD_DIR)))
    file_handle = None

    try:
        await _safe_edit(client, chat_id, message_id, UI.LINK_PROCESSING)
        await _remove_inline_keyboard(client, chat_id, message_id)

        async def on_download_progress(state: dict[str, Any]) -> None:
            downloaded = int(state.get("downloaded") or 0)
            total = int(state.get("total") or 0)
            if not downloaded and not total:
                return
            await _safe_edit(
                client,
                chat_id,
                message_id,
                UI.progress(
                    label=UI.LINK_PROCESSING,
                    percent=int(min(downloaded / total * 100.0, 100.0)) if total else 0,
                    current=human_size(downloaded),
                    total=human_size(total) if total else "—",
                    speed=human_size(state.get("speed")),
                    left=int(state.get("eta") or 0),
                ),
                progress_key=progress_key,
            )

        result = await download_media(
            url,
            mode=mode,
            height=height,
            audio_format=audio_format,
            output_dir=job_dir,
            progress_callback=on_download_progress,
        )

        size_str = human_size(result.filesize)
        await _safe_edit(client, chat_id, message_id, UI.sending_file(result.title, size_str))

        kind = CATEGORY_TITLES.get(mode, CATEGORY_LABELS.get(mode, UI.BUTTON_CATEGORY_VIDEO))
        # aiosqlite auto-numbering: the temp file keeps its name, only the
        # uploaded file_name/title get the per-user suffix (never raises).
        sent_name, sent_title = await database.safe_get_file_suffix(
            user_id,
            result.title,
            result.ext,
            fallback="video" if result.is_video else "audio",
        )
        caption = _build_caption(result, kind, sent_title)
        upload_progress = _make_upload_progress(client, chat_id, message_id)
        file_handle = result.path.open("rb")

        try:
            if result.is_video:
                await client.send_video(
                    chat_id=chat_id,
                    video=file_handle,
                    file_name=sent_name,
                    caption=caption,
                    parse_mode=TEXT_PARSE_MODE,
                    duration=result.duration,
                    supports_streaming=True,
                    progress=upload_progress,
                )
            else:
                await client.send_audio(
                    chat_id=chat_id,
                    audio=file_handle,
                    file_name=sent_name,
                    caption=caption,
                    parse_mode=TEXT_PARSE_MODE,
                    duration=result.duration,
                    title=sent_title[:64],
                    progress=upload_progress,
                )
        except ValueError as exc:
            # Pyrogram raises ValueError for files above the 2000 MiB upload limit.
            if "bigger than" in str(exc):
                raise _SendTooLargeError(str(exc)) from exc
            raise

        await _safe_edit(client, chat_id, message_id, UI.completed(sent_title, size_str))

    except _SendTooLargeError:
        log.warning("Telegram refused the upload for %s (file above 2000 MiB)", url)
        await _safe_edit(client, chat_id, message_id, UI.ERR_TOO_LARGE_TO_SEND)
    except MediaTooLargeError as exc:
        log.warning("Media too large for %s: %s", url, human_size(exc.size))
        await _safe_edit(client, chat_id, message_id, UI.ERR_LINK_TOO_LARGE)
    except DownloadFailed:
        # The detailed reason is logged by downloader.py (LogMessages.LINK_FAIL).
        await _safe_edit(client, chat_id, message_id, UI.ERR_LINK_FAILED)
    except DownloaderError as exc:
        log.exception("Downloader error while processing %s", url)
        await _safe_edit(
            client,
            chat_id,
            message_id,
            UI.ERR_GENERIC.format(details=type(exc).__name__),
        )
    except FloodWait as exc:
        log.warning("FloodWait: %s s while processing %s", exc.value, url)
        await _safe_edit(client, chat_id, message_id, UI.ERR_FLOOD.format(seconds=exc.value))
    except RPCError as exc:
        log.exception("Telegram RPC error while processing %s", url)
        await _safe_edit(
            client,
            chat_id,
            message_id,
            UI.ERR_TELEGRAM_RPC.format(details=type(exc).__name__),
        )
    except Exception as exc:
        log.exception("Unhandled error while processing %s", url)
        await _safe_edit(
            client,
            chat_id,
            message_id,
            UI.ERR_GENERIC.format(details=type(exc).__name__),
        )
    finally:
        # Cleanup guarantee: close the handle and drop the whole job folder.
        if file_handle is not None:
            with contextlib.suppress(Exception):
                file_handle.close()
        shutil.rmtree(job_dir, ignore_errors=True)
        _forget_progress_key(progress_key)
        log.info(LogMessages.JOB_DONE.format(mode=mode, url=url))


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def create_client() -> Client:
    """Build the Pyrogram client and attach every handler.

    The client is created *inside* the running event loop on purpose: Pyrogram
    2.0.x captures ``asyncio.get_event_loop()`` in ``Client.__init__`` and reuses
    that loop to spawn upload tasks.
    """
    client = Client(
        name=config.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workdir=str(config.BASE_DIR),
        workers=config.WORKERS,
        max_concurrent_transmissions=config.MAX_CONCURRENT_TRANSMISSIONS,
        proxy=config.get_proxy(),
    )

    handlers = [
        MessageHandler(cmd_start, filters.command(["start", "help"]) & filters.private),
        MessageHandler(handle_url, filters.private & filters.text & ~filters.command(["start", "help"])),
        CallbackQueryHandler(handle_category_choice, filters.regex(CATEGORY_PATTERN)),
        CallbackQueryHandler(handle_quality_choice, filters.regex(SELECTION_PATTERN)),
        CallbackQueryHandler(handle_back, filters.regex(BACK_PATTERN)),
    ]
    for handler in handlers:
        client.add_handler(handler)

    log.info(
        LogMessages.HANDLERS_LOADED.format(
            handlers=", ".join(handler.callback.__name__ for handler in handlers)
        )
    )

    return client


async def main() -> None:
    """Start the web health server, the self-ping loop and the bot itself."""
    config.validate()
    download_dir = config.ensure_download_dir()
    log.info(LogMessages.DOWNLOAD_DIR.format(path=download_dir))

    # Prepare the per-user file counter table (failures are logged, never fatal).
    await database.init_db()

    # The HTTP listener starts first: Render kills containers whose assigned port
    # never answers ("port scan timeout") and only then cares about the bot.
    health_runner = await start_webserver()

    ping_task: Optional[asyncio.Task[None]] = None
    if config.RENDER_EXTERNAL_URL:
        ping_task = asyncio.create_task(
            self_ping_loop(config.RENDER_EXTERNAL_URL, config.SELF_PING_INTERVAL_SECONDS),
            name="self-ping",
        )
    else:
        log.info(LogMessages.SELF_PING_OFF)

    client = create_client()
    try:
        async with client:
            me = await client.get_me()
            log.info(
                LogMessages.BOT_STARTED.format(
                    username=me.username or str(me.id),
                    limit=f"{config.MAX_FILE_SIZE_MB} MB",
                )
            )
            await idle()
    finally:
        # Shutdown order mirrors startup: stop pinging, then release the port.
        if ping_task is not None:
            ping_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await ping_task
        await stop_webserver(health_runner)

    log.info(LogMessages.STOPPED_IDLE)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )
    # aiohttp/urllib logs every request on INFO, which is noisy on Render.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    try:
        asyncio.run(main())
    except config.ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from exc
    except (KeyboardInterrupt, SystemExit):
        log.info(LogMessages.STOPPED_CTRL_C)

