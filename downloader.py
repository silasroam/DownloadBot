"""Async wrapper around the blocking ``yt-dlp`` API.

Nothing in this module touches the Telegram client: it only downloads media to
disk and reports progress.  The heavy, blocking ``yt-dlp`` call always runs in
the loop's default thread pool executor so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

import config
from strings import LogMessages

try:  # pragma: no cover - yt-dlp is a hard runtime requirement
    import yt_dlp
except ImportError:  # the bot still starts, link downloads are refused with a clear message
    yt_dlp = None

log = logging.getLogger(__name__)
#: yt-dlp's own diagnostics are routed through this logger (see :func:`_build_options`),
#: so the exact failure reason reaches the console instead of being swallowed
#: by ``quiet``/``no_warnings``.
ytdl_logger = logging.getLogger("yt_dlp")

#: ``yt_dlp.utils.DownloadError`` or an empty tuple when yt-dlp is unavailable.
YTDLP_AVAILABLE: bool = yt_dlp is not None
YTDLP_DOWNLOAD_ERROR: tuple = (yt_dlp.utils.DownloadError,) if yt_dlp is not None else ()

if not YTDLP_AVAILABLE:
    log.warning(LogMessages.YTDLP_MISSING)

# --- Download modes ----------------------------------------------------------
#: Video *without* a soundtrack: ``bestvideo[height<=H][ext=mp4]``.
MODE_VIDEO = "video"
#: Audio only: the best soundtrack is extracted and converted by ffmpeg.
MODE_AUDIO = "audio"
#: Full video (picture + sound): video and audio streams are merged into mp4.
MODE_VIDEO_AUDIO = "videoaudio"
VALID_MODES = frozenset({MODE_VIDEO, MODE_AUDIO, MODE_VIDEO_AUDIO})
#: Modes that produce a playable video file (muted or with sound).
VIDEO_MODES = frozenset({MODE_VIDEO, MODE_VIDEO_AUDIO})

#: Quality choices offered by both video categories.
VIDEO_HEIGHTS: tuple[int, ...] = (1080, 720, 480, 360)
#: Audio containers/codecs offered by the audio category.
AUDIO_FORMATS: tuple[str, ...] = ("mp3", "m4a", "aac", "flac")
DEFAULT_HEIGHT = 1080
DEFAULT_AUDIO_FORMAT = "mp3"
#: Bitrate used for lossy audio targets (mp3/opus); ignored by flac/m4a/aac.
AUDIO_QUALITY = "192"
#: Format selector used for audio downloads before the ffmpeg post-processing.
AUDIO_FORMAT = "bestaudio/best"


def build_muted_video_format(height: int = DEFAULT_HEIGHT) -> str:
    """``bestvideo[height<=H][ext=mp4]`` - video only, no audio track."""
    return f"bestvideo[height<={int(height)}][ext=mp4]"


def build_video_with_audio_format(height: int = DEFAULT_HEIGHT) -> str:
    """Video+audio selector, with fallbacks to a single already-muxed stream."""
    height = int(height)
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
        f"/best[height<={height}][ext=mp4]/best"
    )


def is_valid_height(height: object) -> bool:
    """True when ``height`` is one of the offered video qualities."""
    try:
        return int(height) in VIDEO_HEIGHTS
    except (TypeError, ValueError):
        return False


def is_valid_audio_format(audio_format: object) -> bool:
    """True when ``audio_format`` is one of the offered audio codecs."""
    return str(audio_format or "").lower() in AUDIO_FORMATS

#: File extensions that belong to unfinished / auxiliary yt-dlp files.
_TEMP_SUFFIXES = frozenset({".part", ".ytdl", ".temp", ".tmp", ".json"})
#: Characters that are unsafe in a file name.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
#: Human readable units used by :func:`human_size`.
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB")

ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


class DownloaderError(Exception):
    """Base class for every error raised by this module."""


class DownloadFailed(DownloaderError):
    """yt-dlp could not download the media."""


class MediaTooLargeError(DownloaderError):
    """The media exceeds the configured size limit."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"Media is {human_size(size)} which exceeds the {human_size(limit)} limit.")


@dataclass(slots=True)
class DownloadResult:
    """A finished download that lives in a temporary directory."""

    path: Path
    title: str
    duration: int
    filesize: int
    mode: str
    #: Requested video height (``None`` for the audio category).
    height: Optional[int] = None
    #: Requested audio codec (only meaningful for :data:`MODE_AUDIO`).
    audio_format: str = DEFAULT_AUDIO_FORMAT

    @property
    def ext(self) -> str:
        """File extension without the leading dot."""
        suffix = self.path.suffix.lstrip(".").lower()
        if suffix:
            return suffix
        return self.audio_format if self.mode == MODE_AUDIO else "mp4"

    @property
    def is_video(self) -> bool:
        """True for both video categories (muted and with sound)."""
        return self.mode in VIDEO_MODES

    @property
    def has_audio(self) -> bool:
        """False for the muted "video" category."""
        return self.mode in {MODE_AUDIO, MODE_VIDEO_AUDIO}

    @property
    def file_name(self) -> str:
        """Name used when the file is uploaded to Telegram."""
        stem = _safe_file_stem(self.title) or self.path.stem or "media"
        return f"{stem}.{self.ext}"


# ---------------------------------------------------------------------------
# Formatting helpers (shared with the bot UI)
# ---------------------------------------------------------------------------
def human_size(num_bytes: Optional[Union[int, float]]) -> str:
    """Render a byte count as ``12.3 MB``."""
    if not num_bytes or num_bytes <= 0:
        return "0 B"

    value = float(num_bytes)
    for unit in _SIZE_UNITS:
        if value < 1024.0 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def human_time(seconds: Optional[Union[int, float]]) -> str:
    """Render a duration as ``01:23`` or ``1:02:03``."""
    if not seconds or seconds < 0:
        return "00:00"

    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------
async def download_media(
    url: str,
    *,
    mode: str = MODE_VIDEO,
    height: int = DEFAULT_HEIGHT,
    audio_format: str = DEFAULT_AUDIO_FORMAT,
    output_dir: Optional[Path] = None,
    progress_callback: Optional[ProgressCallback] = None,
    progress_interval: float = 2.0,
) -> DownloadResult:
    """Download ``url`` without blocking the event loop.

    ``mode`` selects the category, ``height`` the video quality (both video
    categories) and ``audio_format`` the codec of the audio category.

    The blocking ``yt-dlp`` call runs inside ``run_in_executor``; while it runs,
    a lightweight watcher task forwards progress snapshots to
    ``progress_callback``.
    """
    mode = (mode or "").lower()
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported mode {mode!r}, expected one of {sorted(VALID_MODES)}.")
    if not url or not str(url).strip():
        raise DownloaderError("URL is empty.")

    if mode in VIDEO_MODES:
        if not is_valid_height(height):
            raise ValueError(f"Unsupported height {height!r}, expected one of {list(VIDEO_HEIGHTS)}.")
        height = int(height)
        audio_format = DEFAULT_AUDIO_FORMAT
    else:
        audio_format = str(audio_format or "").lower()
        if not is_valid_audio_format(audio_format):
            raise ValueError(
                f"Unsupported audio format {audio_format!r}, expected one of {list(AUDIO_FORMATS)}."
            )
        height = DEFAULT_HEIGHT

    job_dir = Path(output_dir) if output_dir else Path(config.DOWNLOAD_DIR)
    job_dir.mkdir(parents=True, exist_ok=True)

    state: dict[str, Any] = {
        "status": "queued",
        "downloaded": 0,
        "total": 0,
        "speed": None,
        "eta": None,
    }

    loop = asyncio.get_event_loop()
    watcher: Optional[asyncio.Task[None]] = None
    if progress_callback is not None:
        watcher = asyncio.create_task(_watch_progress(state, progress_callback, progress_interval))

    try:
        # yt-dlp is synchronous: never call it directly from the event loop.
        return await loop.run_in_executor(
            None,
            functools.partial(
                _download_sync, url.strip(), mode, job_dir, state, height, audio_format
            ),
        )
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher


async def _watch_progress(
    state: dict[str, Any],
    callback: ProgressCallback,
    interval: float,
) -> None:
    """Poll the shared state dict and forward snapshots to the callback."""
    while True:
        await asyncio.sleep(interval)
        if state.get("status") not in {"downloading", "finished"}:
            continue
        try:
            await callback(dict(state))
        except asyncio.CancelledError:
            raise
        except Exception:  # a broken UI update must never kill the download
            log.exception("Progress callback failed")


def _download_sync(
    url: str,
    mode: str,
    output_dir: Path,
    state: dict[str, Any],
    height: int = DEFAULT_HEIGHT,
    audio_format: str = DEFAULT_AUDIO_FORMAT,
) -> DownloadResult:
    """Blocking worker executed inside the thread pool executor."""
    if yt_dlp is None:
        raise DownloadFailed("yt-dlp is not installed, link downloads are unavailable.")

    options = _build_options(
        mode, output_dir, state, height=height, audio_format=audio_format
    )

    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except YTDLP_DOWNLOAD_ERROR as exc:  # yt_dlp.utils.DownloadError
        detail = _clean_ytdlp_error(str(exc))
        _log_download_failure(url, exc, detail)
        raise DownloadFailed(detail) from exc
    except OSError as exc:
        _log_download_failure(url, exc, str(exc))
        raise DownloadFailed(f"System error while downloading: {exc}") from exc

    if not info:
        raise DownloadFailed("yt-dlp returned no metadata for this URL.")

    if info.get("_type") == "playlist":
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if not entries:
            raise DownloadFailed("The playlist does not contain any downloadable item.")
        info = entries[0]

    expected_ext = audio_format if mode == MODE_AUDIO else "mp4"
    path = _resolve_output_path(info, output_dir, expected_ext)

    filesize = path.stat().st_size
    # Post-download guard: yt-dlp enforces "max_filesize" while selecting the
    # formats, this catches merged files that still ended up too big.
    if filesize > config.MAX_FILE_SIZE:
        raise MediaTooLargeError(filesize, config.MAX_FILE_SIZE)

    title = _clean_title(info.get("title") or info.get("id") or path.stem)
    duration = int(info.get("duration") or 0)
    state["status"] = "downloaded"
    state["total"] = filesize
    state["downloaded"] = filesize

    return DownloadResult(
        path=path,
        title=title,
        duration=duration,
        filesize=filesize,
        mode=mode,
        height=height if mode in VIDEO_MODES else None,
        audio_format=audio_format,
    )


def _log_download_failure(url: str, exc: BaseException, detail: str) -> None:
    """Log the short reason *and* the full yt-dlp message plus traceback.

    ``quiet``/``no_warnings`` normally hide yt-dlp's output and the user only
    sees a generic error, so the complete cause is written to the console here
    (HTTP status, extractor change, ffmpeg merge failure, missing cookies, ...).
    """
    log.error(
        LogMessages.LINK_FAIL.format(url=url, error_type=type(exc).__name__, error=detail)
    )
    log.error(
        LogMessages.LINK_FAIL_DETAIL.format(url=url, error=str(exc)),
        exc_info=True,
    )


def _build_options(
    mode: str,
    output_dir: Path,
    state: dict[str, Any],
    *,
    height: int = DEFAULT_HEIGHT,
    audio_format: str = DEFAULT_AUDIO_FORMAT,
) -> dict[str, Any]:
    """Translate our settings into a yt-dlp options dictionary."""
    options: dict[str, Any] = {
        # The id keeps file names short, unique and safe; the real title is sent
        # to Telegram through file_name.
        "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        # Warnings are kept (they reach the "yt_dlp" logger) so a failure is not
        # silent; the progress bar itself is still suppressed.
        "no_warnings": False,
        "noprogress": True,
        "consoletitle": False,
        # Some hosts (and corporate proxies) serve broken chains; the bot must
        # not fail because of that.
        "nocheckcertificate": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "overwrites": True,
        "ignoreerrors": False,
        # Bypass geo restrictions whenever the extractor can.
        "geo_bypass": True,
        "progress_hooks": [_progress_hook(state)],
        # Enforced before downloading, during format selection.
        "max_filesize": config.MAX_FILE_SIZE,
        # Route yt-dlp's own diagnostics into Python logging so the exact cause
        # of a failure ends up in the Render/console log.
        "logger": ytdl_logger,
        # A browser-like agent is required by several extractors (YouTube,
        # TikTok, VK) that reject yt-dlp's default User-Agent.
        "http_headers": {
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    }

    if config.FFMPEG_PATH:
        options["ffmpeg_location"] = config.FFMPEG_PATH
    if config.HTTP_PROXY:
        options["proxy"] = config.HTTP_PROXY
    if config.COOKIES_FILE:
        # cookies.txt unlocks age/region/bot-checked videos.
        options["cookiefile"] = config.COOKIES_FILE

    if mode == MODE_AUDIO:
        options["format"] = AUDIO_FORMAT
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": AUDIO_QUALITY,
            }
        ]
    elif mode == MODE_VIDEO_AUDIO:
        options["format"] = build_video_with_audio_format(height)
        options["merge_output_format"] = "mp4"
    else:
        options["format"] = build_muted_video_format(height)
        options["merge_output_format"] = "mp4"

    return options


def _progress_hook(state: dict[str, Any]) -> Callable[[dict[str, Any]], None]:
    """Create a yt-dlp progress hook that writes into ``state``."""

    def hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            state["status"] = "downloading"
            state["total"] = int(total or 0)
            state["downloaded"] = int(data.get("downloaded_bytes") or 0)
            state["speed"] = data.get("speed")
            state["eta"] = data.get("eta")
        elif status == "finished":
            state["status"] = "finished"
            state["total"] = int(state.get("total") or state.get("downloaded") or 0)
            state["downloaded"] = int(state.get("total") or 0)
            state["speed"] = None
            state["eta"] = 0

    return hook


def _resolve_output_path(info: Mapping[str, Any], output_dir: Path, expected_ext: str) -> Path:
    """Find the real file produced by yt-dlp (the extension may change after post-processing)."""
    candidates: list[Path] = []
    for item in info.get("requested_downloads") or []:
        filepath = (item or {}).get("filepath")
        if filepath:
            candidates.append(Path(filepath))

    filepath = info.get("filepath")
    if filepath:
        candidates.append(Path(filepath))

    found = _pick_best(candidates, expected_ext)
    if found is not None:
        return found

    # Post-processing (mp3 extraction, mp4 merge) can rename the file, so look
    # for siblings that share the same stem.
    for candidate in candidates:
        siblings = sorted(candidate.parent.glob(f"{candidate.stem}.*"))
        found = _pick_best(siblings, expected_ext)
        if found is not None:
            return found

    if output_dir.is_dir():
        found = _pick_best(sorted(output_dir.iterdir()), expected_ext)
        if found is not None:
            return found

    raise DownloadFailed("The downloaded file could not be found on disk.")


def _pick_best(paths: Any, expected_ext: str) -> Optional[Path]:
    """Return a finished file, preferring the expected extension and the newest mtime."""
    finished = [
        path
        for path in paths
        if isinstance(path, Path) and path.is_file() and path.suffix.lower() not in _TEMP_SUFFIXES
    ]
    if not finished:
        return None

    preferred = [path for path in finished if path.suffix.lower() == f".{expected_ext}"]
    pool = preferred or finished
    return max(pool, key=lambda path: path.stat().st_mtime)


def _clean_title(title: Any) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip()


def _safe_file_stem(title: str) -> str:
    """Make a title usable as a file name (unsafe characters removed)."""
    stem = _UNSAFE_FILENAME_CHARS.sub(" ", title or "")
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    return stem[:120].strip()


def _clean_ytdlp_error(message: str) -> str:
    """Turn a multi-line yt-dlp error message into a short, readable line."""
    cleaned = re.sub(r"^ERROR:\s*", "", message.strip())
    first_line = cleaned.splitlines()[0] if cleaned else "Unknown yt-dlp error."
    return first_line[:300]
