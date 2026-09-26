"""Shared test setup.

The project is a flat set of modules (main.py, downloader.py, keyboards.py,
database.py, strings.py) without a package, so the project root is added to
``sys.path`` before the test modules import them.
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import pyrocompat  # noqa: E402,F401  (Pyrogram import shim for Python 3.12+)
