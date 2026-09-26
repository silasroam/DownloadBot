"""Unit tests for downloader.py - format builders, yt-dlp options, error logging.

No network is touched: ``yt_dlp.YoutubeDL`` is replaced by :class:`FakeYDL` for
the download tests, while the option dictionaries are validated against the
*real* installed yt-dlp so a typo in an option name fails the suite.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yt_dlp

import config
import downloader


class FakeYDL:
    """Minimal stand-in for ``yt_dlp.YoutubeDL``."""

    captured_opts: dict = {}
    fail = False
    size = 2048
    delay = 0.0
    report_missing_final = False
    thread_name = ""

    def __init__(self, opts: dict) -> None:
        FakeYDL.captured_opts = opts
        self.opts = opts

    def __enter__(self) -> "FakeYDL":
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def extract_info(self, url: str, download: bool = True) -> dict:
        FakeYDL.thread_name = threading.current_thread().name
        if FakeYDL.fail:
            raise yt_dlp.utils.DownloadError("ERROR: [youtube] abc: Video unavailable")

        opts = self.opts
        if "postprocessors" in opts:
            ext = opts["postprocessors"][0]["preferredcodec"]
        else:
            ext = "mp4"
        path = Path(str(opts["outtmpl"]).replace("%(id)s", "abc123").replace("%(ext)s", ext))

        hooks = opts.get("progress_hooks", [])
        steps = max(int(FakeYDL.delay / 0.05), 1)
        for step in range(steps):
            if FakeYDL.delay:
                time.sleep(FakeYDL.delay / steps)
            for hook in hooks:
                hook(
                    {
                        "status": "downloading",
                        "downloaded_bytes": int(FakeYDL.size * (step + 1) / steps),
                        "total_bytes": FakeYDL.size,
                        "speed": 2_000_000.0,
                        "eta": 3,
                    }
                )

        path.write_bytes(b"x" * FakeYDL.size)
        for hook in hooks:
            hook({"status": "finished", "downloaded_bytes": FakeYDL.size, "total_bytes": FakeYDL.size})

        reported = path
        if FakeYDL.report_missing_final:
            reported = path.with_suffix(".webm")  # what yt-dlp reports before post-processing
        return {
            "id": "abc123",
            "title": "Test / Video: «Смешное»  video",
            "duration": 125.0,
            "requested_downloads": [{"filepath": str(reported)}],
        }


@pytest.fixture
def fake_ydl(monkeypatch):
    """Swap in :class:`FakeYDL` and reset its class-level switches."""
    monkeypatch.setattr(downloader.yt_dlp, "YoutubeDL", FakeYDL)
    FakeYDL.fail = False
    FakeYDL.size = 2048
    FakeYDL.delay = 0.0
    FakeYDL.report_missing_final = False
    FakeYDL.thread_name = ""
    return FakeYDL


# ---------------------------------------------------------------------------
# Format builders
# ---------------------------------------------------------------------------
def test_build_muted_video_format_default():
    assert downloader.build_muted_video_format() == "bestvideo[height<=1080][ext=mp4]"


def test_build_muted_video_format_custom_height():
    assert downloader.build_muted_video_format(720) == "bestvideo[height<=720][ext=mp4]"


def test_build_video_with_audio_format():
    assert downloader.build_video_with_audio_format(480) == (
        "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best"
    )


def test_is_valid_height():
    assert downloader.is_valid_height(1080) is True
    assert downloader.is_valid_height("720") is True
    assert downloader.is_valid_height(4320) is False
    assert downloader.is_valid_height(None) is False


def test_is_valid_audio_format():
    assert downloader.is_valid_audio_format("mp3") is True
    assert downloader.is_valid_audio_format("FLAC") is True
    assert downloader.is_valid_audio_format("ogg") is False
    assert downloader.is_valid_audio_format(None) is False


# ---------------------------------------------------------------------------
# Cookies: automatic lookup (Render Secret Files, local fallback) and no proxy
# ---------------------------------------------------------------------------
def test_find_cookies_file_prefers_explicit_config(monkeypatch, tmp_path):
    explicit = tmp_path / "explicit.txt"
    explicit.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(config, "COOKIES_FILE", str(explicit))
    assert downloader._find_cookies_file() == str(explicit)


def test_find_cookies_file_uses_first_existing_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "COOKIES_FILE", "")
    secret = tmp_path / "cookies.txt"
    secret.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(
        downloader,
        "_COOKIES_FILE_CANDIDATES",
        (tmp_path / "missing.txt", secret),
    )
    assert downloader._find_cookies_file() == str(secret)


def test_find_cookies_file_returns_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "COOKIES_FILE", "")
    monkeypatch.setattr(downloader, "_COOKIES_FILE_CANDIDATES", (tmp_path / "missing.txt",))
    assert downloader._find_cookies_file() is None


def test_build_options_sets_cookiefile_and_never_proxy(monkeypatch, tmp_path):
    secret = tmp_path / "cookies.txt"
    secret.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setattr(config, "COOKIES_FILE", "")
    monkeypatch.setattr(downloader, "_COOKIES_FILE_CANDIDATES", (secret,))

    opts = downloader._build_options(downloader.MODE_VIDEO_AUDIO, tmp_path, {})

    assert opts["cookiefile"] == str(secret)
    assert "proxy" not in opts


def test_build_options_omits_cookiefile_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "COOKIES_FILE", "")
    monkeypatch.setattr(downloader, "_COOKIES_FILE_CANDIDATES", (tmp_path / "missing.txt",))

    opts = downloader._build_options(downloader.MODE_VIDEO_AUDIO, tmp_path, {})

    assert "cookiefile" not in opts
    assert "proxy" not in opts
