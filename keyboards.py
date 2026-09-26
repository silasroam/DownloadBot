"""Inline keyboards for the two-step link download menu.

The menu is intentionally split in two steps so the callback data stays tiny
(Telegram allows at most 64 bytes) and the user always sees what they picked:

* **Step 1 - category** (three buttons): muted video, audio only, video+audio.
* **Step 2 - quality/format** (four buttons + "back"): 1080p/720p/480p/360p for
  the video categories, MP3/M4A/AAC/FLAC for the audio category.

Callback data layout::

    cat:<category>:<key>            -> show the step-2 keyboard
    sel:<category>:<value>:<key>    -> start the download
    back:<key>                      -> return to step 1

where ``<category>`` is one of :data:`CATEGORIES`, ``<value>`` is a height
("1080") or an audio format ("mp3") and ``<key>`` is the opaque job key created
by ``main.handle_url``.

This module only arranges the keyboard; every label lives in :mod:`strings`.
"""

from __future__ import annotations

import re
from typing import Iterable

import pyrocompat  # noqa: F401  (installs the Pyrogram import shim)

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from downloader import (
    AUDIO_FORMATS,
    MODE_AUDIO,
    MODE_VIDEO,
    MODE_VIDEO_AUDIO,
    VIDEO_HEIGHTS,
)
from strings import UI

#: Category codes (kept identical to the yt-dlp modes for a direct mapping).
CATEGORY_VIDEO = MODE_VIDEO
CATEGORY_AUDIO = MODE_AUDIO
CATEGORY_VIDEO_AUDIO = MODE_VIDEO_AUDIO
#: Order in which the three main buttons are shown.
CATEGORIES: tuple[str, ...] = (CATEGORY_VIDEO, CATEGORY_AUDIO, CATEGORY_VIDEO_AUDIO)

#: Short label shown on the main button.
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_VIDEO: UI.BUTTON_CATEGORY_VIDEO,
    CATEGORY_AUDIO: UI.BUTTON_CATEGORY_AUDIO,
    CATEGORY_VIDEO_AUDIO: UI.BUTTON_CATEGORY_VIDEO_AUDIO,
}
#: Long label used in the step-2 message header.
CATEGORY_TITLES: dict[str, str] = {
    CATEGORY_VIDEO: UI.CATEGORY_VIDEO_TITLE,
    CATEGORY_AUDIO: UI.CATEGORY_AUDIO_TITLE,
    CATEGORY_VIDEO_AUDIO: UI.CATEGORY_VIDEO_AUDIO_TITLE,
}

#: Callback data prefixes.
CALLBACK_CATEGORY = "cat"
CALLBACK_SELECT = "sel"
CALLBACK_BACK = "back"

_KEY = r"[A-Za-z0-9_-]{4,64}"
_CATEGORY = "|".join(CATEGORIES)

#: ``cat:<category>:<key>`` - step 1 button pressed.
CATEGORY_PATTERN = re.compile(
    rf"^{CALLBACK_CATEGORY}:(?P<category>{_CATEGORY}):(?P<key>{_KEY})$"
)
#: ``sel:<category>:<value>:<key>`` - step 2 button pressed.
SELECTION_PATTERN = re.compile(
    rf"^{CALLBACK_SELECT}:(?P<category>{_CATEGORY}):(?P<value>[a-z0-9]+):(?P<key>{_KEY})$"
)
#: ``back:<key>`` - "back" button pressed.
BACK_PATTERN = re.compile(rf"^{CALLBACK_BACK}:(?P<key>{_KEY})$")


def is_valid_category(category: object) -> bool:
    """True for the three supported category codes."""
    return str(category or "") in CATEGORIES


def category_callback(category: str, key: str) -> str:
    """Callback data payload for a step-1 button."""
    return f"{CALLBACK_CATEGORY}:{category}:{key}"


def selection_callback(category: str, value: str, key: str) -> str:
    """Callback data payload for a step-2 button."""
    return f"{CALLBACK_SELECT}:{category}:{value}:{key}"


def back_callback(key: str) -> str:
    """Callback data payload for the "back" button."""
    return f"{CALLBACK_BACK}:{key}"


def selection_options(category: str) -> tuple[tuple[str, str], ...]:
    """Return ``(value, label)`` pairs for the step-2 buttons of ``category``.

    Video categories expose the four heights, the audio category the four
    codecs.  Values are the exact strings used in callback data and passed to
    :func:`downloader.download_media`.
    """
    if category == CATEGORY_AUDIO:
        return tuple((codec, UI.AUDIO_BUTTON_LABELS[codec]) for codec in AUDIO_FORMATS)
    return tuple((str(height), UI.QUALITY_BUTTON_LABELS[height]) for height in VIDEO_HEIGHTS)


def _rows(buttons: Iterable[InlineKeyboardButton]) -> list[list[InlineKeyboardButton]]:
    return [[button] for button in buttons]


def category_keyboard(key: str) -> InlineKeyboardMarkup:
    """Step 1: the three category buttons, one per row."""
    return InlineKeyboardMarkup(
        _rows(
            InlineKeyboardButton(label, callback_data=category_callback(category, key))
            for category, label in CATEGORY_LABELS.items()
        )
    )


def quality_keyboard(category: str, key: str) -> InlineKeyboardMarkup:
    """Step 2: four quality/format buttons in one row plus a "back" button."""
    row = [
        InlineKeyboardButton(label, callback_data=selection_callback(category, value, key))
        for value, label in selection_options(category)
    ]
    back = InlineKeyboardButton(UI.BUTTON_BACK, callback_data=back_callback(key))
    return InlineKeyboardMarkup([row, [back]])
