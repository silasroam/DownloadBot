"""Per-user file numbering backed by SQLite (aiosqlite).

A small standalone module (like :mod:`downloader`): ``main.py`` only imports
functions from here.  Every operation is asynchronous and **never** raises -
if the database is unavailable the upload continues with a safe, unnumbered
file name and the problem is written to the log.

Schema::

    users_files(user_id INTEGER PRIMARY KEY, file_count INTEGER DEFAULT 0)

Numbering (``file_count`` before the increment)::

    0 -> Track.mp4
    1 -> Track_1.mp4
    2 -> Track_2.mp4
    ...
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from strings import LogMessages

try:  # the bot must keep working even if aiosqlite is missing
    import aiosqlite
except ImportError:  # pragma: no cover - depends on the environment
    aiosqlite = None

log = logging.getLogger(__name__)

#: The database lives next to the code and is created automatically at startup.
DB_PATH = Path(__file__).resolve().parent / "bot_database.db"

#: Media extensions stripped when computing the base name of a file.
MEDIA_EXTS = {
    ".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".aac", ".flac", ".wma",
    ".aiff", ".aif", ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp",
    ".mpeg", ".mpg", ".bin",
}

#: Serializes read-modify-write so two files of one user never share a number.
_DB_LOCK = asyncio.Lock()

#: Auto-generated names (``video_2026-09-25_12-30-00``, digits or hex garbage)
#: are not considered meaningful - a fallback word is used instead.
GENERIC_NAME_RE = re.compile(
    r"^(?:video|audio|voice|video_note|animation|document|photo|sticker|gif)"
    r"_\d{4}-\d{2}-\d{2}[_ ]\d{2}-\d{2}-\d{2}(?:[_ ]\d+)?$"
    r"|^\d{6,}$"
    r"|^[0-9a-f]{16,40}$",
    re.IGNORECASE,
)

#: Control characters and path separators are unsafe in a file name.
UNSAFE_NAME_RE = re.compile(r"[\x00-\x1f\x7f/\\]+")


def is_generic_name(name: str) -> bool:
    """True for auto-generated names (``video_2026-09-25_12-30-00``, digits/hex)."""
    return bool(GENERIC_NAME_RE.match((name or "").strip()))


def clean_stem(original_filename: str | None, fallback: str = "media") -> str:
    """Base name without extension.

    Empty, unsafe or auto-generated names are replaced with ``fallback`` - a
    generic word for the media kind (video/audio/media).
    """
    fallback = (fallback or "").strip() or "media"
    name = (original_filename or "").strip()
    if name:
        suffix = Path(name).suffix.lower()
        if suffix in MEDIA_EXTS:  # strip only known media extensions
            name = name[: -len(suffix)]
        name = UNSAFE_NAME_RE.sub("_", name).strip().strip(".")

    if not name or is_generic_name(name):
        return fallback
    return name


async def init_db(db_path: Path | str = DB_PATH) -> None:
    """Create the ``users_files`` table if it does not exist yet. Errors are logged."""
    if aiosqlite is None:
        log.warning(LogMessages.DB_NO_AIOSQLITE)
        return

    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users_files (
                    user_id    INTEGER PRIMARY KEY,
                    file_count INTEGER DEFAULT 0
                )
                """
            )
            await db.commit()
        log.info(LogMessages.DB_READY.format(db_path=db_path))
    except Exception as exc:  # the DB must not stop the bot from starting
        log.warning(LogMessages.DB_INIT_FAIL.format(error_type=type(exc).__name__, error=exc))


async def get_and_increment_file_name(
    user_id: int,
    original_filename: str | None,
    extension: str,
    fallback: str = "media",
) -> tuple[str, str]:
    """Return ``(file_name_with_suffix, pretty_title)`` and increment the counter.

    ``original_filename`` is the source title, ``fallback`` the generic word used
    when it is missing/auto-generated (see :func:`clean_stem`).

    Without a working database the name is returned without a suffix so the
    numbering never lies and the upload never fails.
    """
    clean_name = clean_stem(original_filename, fallback)
    ext = (extension or "").lstrip(".")

    if aiosqlite is None:
        stem = clean_name
        return (f"{stem}.{ext}" if ext else stem), stem

    try:
        async with _DB_LOCK:
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute(
                    "SELECT file_count FROM users_files WHERE user_id = ?", (user_id,)
                ) as cursor:
                    row = await cursor.fetchone()

                if row is None:
                    count = 0
                    await db.execute(
                        "INSERT INTO users_files (user_id, file_count) VALUES (?, 1)",
                        (user_id,),
                    )
                else:
                    count = int(row[0])
                    await db.execute(
                        "UPDATE users_files SET file_count = file_count + 1 WHERE user_id = ?",
                        (user_id,),
                    )
                await db.commit()
    except Exception as exc:  # disk/network/lock - never break the download
        log.warning(LogMessages.DB_NAME_FAIL.format(error_type=type(exc).__name__, error=exc))
        stem = clean_name
        return (f"{stem}.{ext}" if ext else stem), stem

    suffix = "" if count == 0 else f"_{count}"
    stem = f"{clean_name}{suffix}"
    return (f"{stem}.{ext}" if ext else stem), stem


async def safe_get_file_suffix(
    user_id: int,
    title: str | None,
    extension: str = "mp4",
    fallback: str = "media",
) -> tuple[str, str]:
    """Safe wrapper around :func:`get_and_increment_file_name` that never raises.

    Used when the name is built from source metadata (e.g. a yt-dlp title)
    rather than from an uploaded file: ``Track`` -> ``Track.mp4`` ->
    ``Track_1.mp4`` -> ``Track_2.mp4``.
    """
    try:
        return await get_and_increment_file_name(
            user_id, title, extension, fallback=fallback
        )
    except Exception as exc:  # a DB hiccup must never drop the upload
        log.warning(LogMessages.DB_NAME_FAIL.format(error_type=type(exc).__name__, error=exc))
        stem = clean_stem(title, fallback)
        ext = (extension or "").lstrip(".")
        return (f"{stem}.{ext}" if ext else stem), stem
