"""Python 3.12+ import shim for Pyrogram.

Pyrogram 2.0.x runs ``asyncio.get_event_loop()`` while importing
``pyrogram/sync.py``.  Since Python 3.12 that call no longer creates a loop
implicitly (and raises ``RuntimeError`` on 3.14), which breaks
``import pyrogram`` completely.  Installing a placeholder loop before the
import keeps Pyrogram importable; the real loop is created later by
``asyncio.run()``.

Import this module *before* any ``pyrogram`` import (``main.py`` and
``keyboards.py`` do exactly that).
"""

from __future__ import annotations

import asyncio
import warnings


def install() -> None:
    """Create a placeholder event loop when none is available."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.set_event_loop(asyncio.new_event_loop())


install()
