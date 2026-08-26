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


# --------------------------------------------------------------------------
# Text files
# --------------------------------------------------------------------------
# Every text file this tool reads or writes is UTF-8, said once, here.
#
# Python's default is the *locale* encoding, which on the desk machine this is
# built for is cp1252 -- and cp1252 cannot decode a byte of a UTF-8 sequence
# for a character it has no room for.  That is not a hypothetical: reading
# ``volkit/web/index.html`` with the default stopped the Windows build dead
# with ``'charmap' codec can't decode byte 0x81``, and the page is full of the
# same em dashes and greek letters as everything else here.  A file the tool
# wrote on one platform must read back on the other.
#
# Reading uses ``utf-8-sig`` and writing plain ``utf-8``: Notepad and Excel
# both put a byte order mark on what they save, and left in place it becomes
# part of the first key of a settings file or the first pair name of a feed --
# a header nobody typed and nothing matches.  Stripping one that is there is
# free; writing one is not, so it is never written.
READ_ENCODING = "utf-8-sig"
WRITE_ENCODING = "utf-8"


def read_text(path: str | Path) -> str:
    """Read a whole text file as UTF-8, naming the file if it is not."""
    data = Path(path).read_bytes()
    try:
        return data.decode(READ_ENCODING)
    except UnicodeDecodeError as exc:
        raise UnicodeDecodeError(exc.encoding, exc.object, exc.start, exc.end,
                                 f"{exc.reason} -- {path} is not UTF-8 text") from None


def write_text(path: str | Path, text: str) -> None:
    """Write a whole text file as UTF-8, with no byte order mark."""
    Path(path).write_text(text, encoding=WRITE_ENCODING)


def open_text(path: str | Path, **kw):
    """Open a text file for reading as UTF-8; for the csv readers, which want
    a handle rather than a string."""
    return Path(path).open(encoding=READ_ENCODING, **kw)
