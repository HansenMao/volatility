"""The marks a session has made, written to a file and put back.

The workbook is the book of record and stays that way (§2): everything the
marking and market-maker screens do -- a re-marked backbone, a correlation
term structure, an event schedule, a tenor overwrite, a smile parameter
overwrite, a wing shift out of a broker run, a band treatment -- lives on the
loaded book in memory, and a reload discards it.  That is the right default
for a tool whose primary file is somebody else's spreadsheet, and it is the
wrong thing to do to a morning's work at 5pm.

So a session can be *saved beside* the workbook rather than into it.  The file
is the tool's own, like the knowledge bank: JSON, atomically written, and
readable by the person whose marks are in it.

Three things are decided here once.

**The file holds what the screen shows.**  Volatility numbers are in
volatility points, the way they are typed and read on the marking screen; the
shape parameters, decays and correlations are the raw numbers those fields
carry.  ``curve_params`` and ``set_curve_params`` are the one conversion, and
the marking screen uses the same two functions, so the file cannot drift out
of step with the panel that wrote it (§4, "volatility points at the edges").

**Loading replaces, and says what it replaced.**  Overwrites and events are
cleared before the saved ones go on, because a merge would leave a tenor
overwritten by a session nobody remembers.  A pair in the file that this
workbook does not build is reported, not skipped quietly; a pair in the book
that the file does not mention is left exactly as it is, and that is reported
too.

**A pair that will not take its marks does not take the rest down with it.**
Each pair is applied inside its own guard and each failure is collected with
the pair's name on it, in the same spirit as every other table in this
project: the row keeps its place and carries its reason.

Pairs are applied in the book's own build order, so a cross gets its legs'
marks before it recalibrates against them.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .banded import BandTreatment
from .cross import CrossAtmCurve
from .paths import WRITE_ENCODING, app_dir, read_text
from .surface import PARAM_NAMES
from .timeutil import parse_datetime

#: Bumped when the shape changes in a way an older build cannot read.
SESSION_VERSION = 1

#: The default name, beside the workbook -- a session is the user's data.
SESSION_FILENAME = "vol_session.json"

#: What every block of a pair may carry.  Listed once so the reader, the
#: writer and the tests all agree on the vocabulary.
PAIR_BLOCKS = ("curve", "events", "atm_overwrites", "smile_overwrites",
               "param_shifts", "anchor_tenors", "band")

UNITS_NOTE = ("volatility numbers are in volatility points, as they are typed on the "
              "marking screen; shape parameters, decays, correlations and smile "
              "parameters are the raw numbers those fields carry")


class SessionError(ValueError):
    """A session file that cannot be read, or one that is not a session file."""


def default_path() -> Path:
    """Beside the user's workbook, like the knowledge bank."""
    return app_dir() / SESSION_FILENAME


# --------------------------------------------------------------------------
# the ATM curve, in the units the screen uses
# --------------------------------------------------------------------------
def curve_params(atm) -> dict:
    """The backbone (or a cross's correlation) as the marking screen shows it.

    Volatility fields in points, everything else raw.  The screen and the
    session file read the same function so the two cannot disagree about what
    a number means.
    """
    if isinstance(atm, CrossAtmCurve):
        return {
            "corr_initial": atm.correlation.initial,
            "corr_final": atm.correlation.final,
            "corr_decay": atm.correlation.decay,
            "short_addon": atm.params.short_addon * 100.0,
            "short_decay": atm.params.short_decay,
        }
    p = atm.params
    return {
        "initial_vol": p.initial_vol * 100.0,
        "long_term_vol": p.long_term_vol * 100.0,
        "mean_reversion": p.mean_reversion,
        "short_addon": p.short_addon * 100.0,
        "short_decay": p.short_decay,
        "rate_vol": p.rate_vol * 100.0,
        "rate_corr": p.rate_corr,
    }


def set_curve_params(atm, vals: dict) -> list[str]:
    """Put the screen's own numbers back on a curve.  Returns any problems.

    A field left out keeps its current value rather than becoming zero, which
    is what lets a saved file written by an older build -- or a partial edit
    from the panel -- move only what it actually names.
    """
    if isinstance(atm, CrossAtmCurve):
        problems = atm.set_correlation(
            vals.get("corr_initial", atm.correlation.initial),
            vals.get("corr_final", atm.correlation.final),
            vals.get("corr_decay", atm.correlation.decay))
        if problems:
            return problems
        return atm.set_params(
            short_addon=vals.get("short_addon", atm.params.short_addon * 100.0) / 100.0,
            short_decay=vals.get("short_decay", atm.params.short_decay))
    changes: dict[str, float] = {}
    for key in ("initial_vol", "long_term_vol", "short_addon", "rate_vol"):
        if key in vals:
            changes[key] = vals[key] / 100.0
    for key in ("mean_reversion", "short_decay", "rate_corr"):
        if key in vals:
            changes[key] = vals[key]
    return atm.set_params(**changes)


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------
def capture_pair(book, pair: str) -> dict:
    """Everything a session has marked on one pair."""
    surface = book[pair]
    atm = surface.atm
    block: dict = {
        "curve": curve_params(atm),
        "events": [{"when": e.when.strftime("%Y-%m-%dT%H:%M"),
                    "bump": e.bump * 100.0,
                    "label": e.label} for e in atm.events.events],
        "atm_overwrites": {k: v * 100.0 for k, v in sorted(atm.tenor_overwrites.items())},
        "smile_overwrites": {name: dict(sorted(ow.items()))
                             for name, ow in sorted(surface.param_overwrites.items()) if ow},
        "param_shifts": {k: float(v) for k, v in sorted(surface.param_shifts.items()) if v},
        "anchor_tenors": bool(surface.anchor_tenors),
    }
    # The treatment is only written for a pair that has a band or has been
    # marked away from the default.  Writing it for every free floater would
    # put a hazard rate in the file for pairs that have no peg to break.
    treatment = surface.band_treatment
    if surface.band is not None or treatment != BandTreatment():
        block["band"] = treatment.to_request()
    return block


def capture(book, pairs=None, *, note: str = "") -> dict:
    """The whole session as a document, ready to be written.

    ``pairs`` narrows it; the default is every pair the book built.  The
    header records the workbook and the valuation clock the marks were made
    against -- neither is enforced on the way back in, because a marker
    re-opening yesterday's marks against today's clock is the normal case, but
    a file that came from a different workbook is worth being told about.
    """
    if book is None:
        raise SessionError("there is no loaded book to save marks from")
    wanted = [p for p in book.pairs if pairs is None or p.upper() in
              {str(x).upper() for x in pairs}]
    unknown = ([] if pairs is None else
               [str(x).upper() for x in pairs if str(x).upper() not in
                {p.upper() for p in book.pairs}])
    if unknown:
        raise SessionError(f"{', '.join(unknown)} is not built in this book; it holds "
                           f"{', '.join(book.pairs)}")
    return {
        "kind": "volkit session",
        "version": SESSION_VERSION,
        "saved": datetime.now().astimezone().replace(microsecond=0).isoformat(),
        "valuation": book.clock.now.isoformat(),
        "workbook": str(getattr(book.data, "source", "") or ""),
        "note": str(note or ""),
        "units": UNITS_NOTE,
        "pairs": {p: capture_pair(book, p) for p in wanted},
    }


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def _apply_pair(surface, block: dict) -> list[str]:
    """One pair's marks, in the order the screen would make them.

    Curve first, because the events calibrate against it; then the overwrites,
    which sit on top of whatever the curve produced; then the wing shifts and
    the band treatment.  Every step that can refuse a value returns its
    reasons and they are collected rather than raised, so a file with one bad
    number still restores everything else and says what it could not.
    """
    problems: list[str] = []
    if isinstance(block.get("curve"), dict):
        vals = {}
        for k, v in block["curve"].items():
            if v in (None, ""):
                continue
            try:
                vals[k] = float(v)
            except (TypeError, ValueError):
                problems.append(f"curve parameter {k}: {v!r} is not a number")
        problems.extend(set_curve_params(surface.atm, vals))

    if "events" in block:
        entries = []
        for i, row in enumerate(block.get("events") or [], start=1):
            try:
                when = parse_datetime(str(row["when"]))
                bump = float(row.get("bump") or 0.0) / 100.0
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"event {i}: {exc}")
                continue
            entries.append((when, bump, str(row.get("label") or "")))
        # Replace, never merge: the saved schedule is the whole schedule, and
        # adding to whatever the workbook already had would double every
        # release that appears in both.
        problems.extend(surface.atm.set_events(entries))

    if "atm_overwrites" in block:
        surface.atm.clear_overwrite()
        for tenor, value in (block.get("atm_overwrites") or {}).items():
            try:
                surface.atm.overwrite_tenor(str(tenor), float(value) / 100.0)
            except (TypeError, ValueError):
                problems.append(f"ATM overwrite {tenor}: {value!r} is not a number")

    if "smile_overwrites" in block:
        surface.clear_param_overwrites()
        for name, rows in (block.get("smile_overwrites") or {}).items():
            if name not in PARAM_NAMES:
                problems.append(f"smile overwrite: {name!r} is not a smile parameter "
                                f"({', '.join(PARAM_NAMES)})")
                continue
            for tenor, value in (rows or {}).items():
                try:
                    surface.overwrite_param(name, str(tenor), float(value))
                except (TypeError, ValueError):
                    problems.append(f"smile overwrite {name} {tenor}: {value!r} is not a number")

    if "param_shifts" in block:
        shifts = {}
        for name, value in (block.get("param_shifts") or {}).items():
            if name not in PARAM_NAMES:
                problems.append(f"wing shift: {name!r} is not a smile parameter "
                                f"({', '.join(PARAM_NAMES)})")
                continue
            try:
                shifts[name] = float(value)
            except (TypeError, ValueError):
                problems.append(f"wing shift {name}: {value!r} is not a number")
        problems.extend(surface.set_param_shifts(shifts))

    if "anchor_tenors" in block:
        surface.anchor_tenors = bool(block["anchor_tenors"])

    if isinstance(block.get("band"), dict):
        try:
            problems.extend(surface.set_band_treatment(
                BandTreatment.from_request(block["band"])))
        except (TypeError, ValueError) as exc:
            problems.append(f"band treatment: {exc}")

    surface.invalidate()
    return problems


def apply_document(book, doc: dict, pairs=None) -> dict:
    """Put a saved session back onto a loaded book.

    Returns what happened rather than raising: which pairs took their marks,
    which the workbook does not build, which it built and the file never
    mentioned, and every problem with the pair's name on it.
    """
    if book is None:
        raise SessionError("there is no loaded book to put marks onto")
    if not isinstance(doc, dict) or not isinstance(doc.get("pairs"), dict):
        raise SessionError("that is not a volkit session file: it has no 'pairs' object")

    version = int(doc.get("version") or 1)
    notes: list[str] = []
    if version > SESSION_VERSION:
        notes.append(
            f"this file was written by a newer volkit (format {version}, this build reads "
            f"{SESSION_VERSION}); anything it does not understand is left alone")
    saved_book = str(doc.get("workbook") or "")
    here = str(getattr(book.data, "source", "") or "")
    if saved_book and here and Path(saved_book).name != Path(here).name:
        notes.append(f"these marks were saved against {Path(saved_book).name}, and the loaded "
                     f"workbook is {Path(here).name}; the tenors and pairs may not line up")

    wanted = {k.upper() for k in doc["pairs"]} if pairs is None else \
        {str(x).upper() for x in pairs}
    have = {p.upper(): p for p in book.pairs}
    problems: list[str] = []
    applied: list[str] = []

    missing = sorted(k for k in doc["pairs"] if k.upper() in wanted and k.upper() not in have)
    if missing:
        problems.append(f"the file has marks for {', '.join(missing)}, which this workbook "
                        f"does not build; they were not applied")

    # Build order, so a cross recalibrates against legs that already have
    # their saved marks rather than against the workbook's.
    order = [p for p in book.build_order() if p in have.values()]
    order += [p for p in book.pairs if p not in order]
    by_upper = {k.upper(): v for k, v in doc["pairs"].items()}
    for pair in order:
        key = pair.upper()
        if key not in wanted or key not in by_upper:
            continue
        block = by_upper[key]
        if not isinstance(block, dict):
            problems.append(f"{pair}: its entry in the file is not an object")
            continue
        try:
            problems.extend(f"{pair}: {x}" for x in _apply_pair(book[pair], block))
        except Exception as exc:  # noqa: BLE001 - one pair, not the whole file
            problems.append(f"{pair}: {type(exc).__name__}: {exc}")
            continue
        applied.append(pair)

    untouched = [p for p in book.pairs if p.upper() not in by_upper]
    if untouched:
        notes.append(f"the file says nothing about {', '.join(untouched)}; "
                     f"{'they were' if len(untouched) > 1 else 'it was'} left as the workbook "
                     f"has {'them' if len(untouched) > 1 else 'it'}")
    return {"applied": applied, "problems": problems, "notes": notes,
            "saved": str(doc.get("saved") or ""), "note": str(doc.get("note") or ""),
            "workbook": saved_book, "valuation": str(doc.get("valuation") or "")}


# --------------------------------------------------------------------------
# io
# --------------------------------------------------------------------------
def load(path: str | Path) -> dict:
    """Read a session file.  A missing or unreadable one is an error, not an
    empty session: somebody asked for these marks by name."""
    p = Path(path)
    if not p.exists():
        raise SessionError(f"no session file at {p}")
    try:
        raw = json.loads(read_text(p))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionError(f"cannot read the session file at {p}: {exc}") from None
    if not isinstance(raw, dict) or "pairs" not in raw:
        raise SessionError(f"{p} is not a volkit session file; expected an object with a "
                           f"'pairs' key, got {type(raw).__name__}")
    return raw


def write(doc: dict, path: str | Path | None = None) -> str:
    """Write a document atomically, so an interrupted save cannot lose the file."""
    p = Path(path) if path else default_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".vol_session", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding=WRITE_ENCODING) as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return str(p)


def save(book, path: str | Path | None = None, pairs=None, *, note: str = "") -> str:
    """Capture the session and write it.  Returns the file written."""
    return write(capture(book, pairs, note=note), path)


def restore(book, path: str | Path, pairs=None) -> dict:
    """Read a session file and put it onto the book, in one step."""
    out = apply_document(book, load(path), pairs)
    out["path"] = str(path)
    return out
