"""Where things live, both when running from source and when frozen into an exe.

PyInstaller unpacks bundled resources to a temporary directory and leaves the
user's own files next to the executable.  Those are different places and must
not be confused: the web page and the shipped event calendar travel *inside*
the bundle, while the workbook, the feed and the holiday overrides are the
user's and live beside the exe where they can be edited.
"""

from __future__ import annotations

import codecs
import io
import locale
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


#: Every file that turned out not to be UTF-8, and what it was read as
#: instead.  Appended to by :func:`decode_text`; ``cli.main`` prints whatever
#: is in it.  A file read in an encoding nobody chose is not an error -- it is
#: the file a person actually saved -- but it is not silent either, because
#: the *next* thing it does is round-trip through this tool as UTF-8 and stop
#: matching what Notepad shows.
ENCODING_NOTES: list[str] = []


def ansi_encoding() -> str:
    """This machine's own ANSI code page: what its Notepad writes by default.

    ``cp1252`` on an English Windows, ``cp936`` on a Chinese one, ``utf-8`` on
    the Mac and on Linux -- which is why the fallback below is a *no-op* off
    Windows, and deliberately so. Guessing between code pages is worse than
    not falling back at all: ``cp1252`` decodes any byte sequence at all, so
    trying it on a ``cp936`` file always "works" and always produces mojibake.
    The one code page that is a fact rather than a guess is the one belonging
    to the machine that saved the file, which for a desk's own settings file
    is this one.
    """
    return locale.getpreferredencoding(False) or "cp1252"


def decode_text(data: bytes, source: str | Path = "", *, ansi: str = "") -> tuple[str, str]:
    """Bytes into text, and a note when it took more than UTF-8 to do it.

    UTF-8 is what this tool writes and what it asks for, so it is tried first
    -- with the byte order mark stripped, because Notepad and Excel both put
    one on what they save and left in place it becomes part of the first key
    of a settings file or the first pair name of a feed.

    Then two fallbacks, in this order, for files a **person** typed:

    * **UTF-16**, but only when the file carries a UTF-16 byte order mark.
      That is Notepad's "Unicode" option and it is unmistakable; guessing at
      UTF-16 without the mark is how an ASCII file becomes Chinese.
    * **The machine's own ANSI code page** -- ``cp1252`` on an English
      Windows, ``cp936`` on a Chinese one -- which is what Notepad's "ANSI"
      writes and what it wrote by default for years.  This is the fallback
      that matters: a ``volkit.cfg`` with a Chinese path in it, saved the way
      the machine saves by default, used to stop the exe at startup with a
      decode error instead of reading the path.

    The note names what happened; refusing the file outright and reading it
    silently are both worse than reading it and saying so.
    """
    for bom, enc in ((codecs.BOM_UTF8, "utf-8-sig"),
                     (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16")):
        if data.startswith(bom):
            try:
                text = data.decode(enc)
            except UnicodeDecodeError:
                break
            note = "" if enc == "utf-8-sig" else \
                f"{source or 'the file'} is UTF-16 (Notepad's 'Unicode'); it was read as that"
            if note and note not in ENCODING_NOTES:
                ENCODING_NOTES.append(note)
            return text, note
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError as exc:
        first = exc

    ansi = ansi or ansi_encoding()
    normal = codecs.lookup(ansi).name if _known(ansi) else ""
    try:
        text = data.decode(ansi) if normal and normal != "utf-8" else None
    except UnicodeDecodeError:
        text = None
    if text is None:
        # Nothing read it.  The UTF-8 failure is the one worth quoting: it is
        # the encoding this tool asked for, and on a machine whose own code
        # page *is* UTF-8 there was never a second reading to try.
        raise UnicodeDecodeError(
            first.encoding, first.object, first.start, first.end,
            f"{first.reason} -- {source or 'the file'} is not UTF-8"
            + (f" and this machine's code page ({normal}) does not read it either"
               if normal and normal != "utf-8" else "")
            + "; save it as UTF-8 (Notepad: Save as, Encoding: UTF-8)") from None
    note = (f"{source or 'the file'} is not UTF-8; it was read as {ansi}, this "
            f"machine's ANSI code page. Re-save it as UTF-8 to be sure of it "
            f"on another machine")
    if note not in ENCODING_NOTES:
        ENCODING_NOTES.append(note)
    return text, note


def _known(name: str) -> bool:
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    return True


def read_text(path: str | Path) -> str:
    """Read a whole text file, UTF-8 first (:func:`decode_text`)."""
    return decode_text(Path(path).read_bytes(), path)[0]


def write_text(path: str | Path, text: str) -> None:
    """Write a whole text file as UTF-8, with no byte order mark."""
    Path(path).write_text(text, encoding=WRITE_ENCODING)


def open_text(path: str | Path, **kw):
    """A text file as a handle, for the csv readers, which want one.

    Read whole and decoded through :func:`decode_text` rather than opened, so
    a csv gets the same fallback ladder a settings file does -- and so the
    file itself is shut before parsing starts, which is the rule every other
    reader here follows (``marketdata.open_workbook``).
    """
    # ``newline=""`` is what all three callers ask for and what ``csv`` wants:
    # no translation, so a quoted field with a line break inside it survives.
    kw.setdefault("newline", "")
    return io.StringIO(read_text(path), **kw)


def use_utf8_streams() -> None:
    """Speak UTF-8 on the standard streams, whatever the machine's locale is.

    Said here, beside the file encoding, and called before anything prints.
    The tables printed by this tool are full of em dashes and greek letters, a
    tile's label carries an arrow, and a desk's workbook may be under a path
    written in Chinese.  Interactively Windows writes those through the
    console's own Unicode call and all is well, but the moment the output is
    piped or redirected Python falls back to the locale encoding -- cp1252 on
    the desk -- and one arrow ends the run with a ``UnicodeEncodeError``
    instead of a table.  A packaged app printing the path it just read is
    enough: that is how a Chinese workbook name stopped the exe before it had
    done anything at all.

    A redirected *input* is the same story: a broker run saved out of a chat
    window is UTF-8, and read as cp1252 the quotes come back as mojibake.
    ``errors="replace"`` on the way in, because a byte this tool cannot read
    is one bad character on one line and is visible there, not a reason to
    refuse a whole run.
    """
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "backslashreplace"),
                           (sys.stderr, "backslashreplace")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError, OSError):
            # Not a real stream (a test's StringIO), or one somebody has
            # already wrapped.  The encoding is then not ours to set -- but
            # the error handling still is, and it is the half that decides
            # whether an unprintable character ends the run or prints as
            # itself escaped.
            try:
                stream.reconfigure(errors=errors)
            except (AttributeError, ValueError, OSError):
                pass
