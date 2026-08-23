"""Where things live, both when running from source and when frozen into an exe.

PyInstaller unpacks bundled resources to a temporary directory and leaves the
user's own files next to the executable.  Those are different places and must
not be confused: the web page and the shipped event calendar travel *inside*
the bundle, while the workbook, the feed and the holiday overrides are the
user's and live beside the exe where they can be edited.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_dir() -> Path:
    """Directory holding bundled, read-only resources."""
    if is_frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """Directory the user keeps their data in.

    Beside the executable when frozen, the project root otherwise.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def find_data_file(*candidates: str) -> Path | None:
    """First existing candidate, searched relative to the app directory then cwd."""
    for name in candidates:
        for base in (Path.cwd(), app_dir()):
            p = (base / name) if not Path(name).is_absolute() else Path(name)
            if p.exists():
                return p
    return None
