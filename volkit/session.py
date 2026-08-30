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
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .banded import BandTreatment
from .cross import is_cross
from .cross import CrossAtmCurve
from .paths import WRITE_ENCODING, app_dir, read_text
from .surface import PARAM_NAMES
from .events import event_entries
from .timeutil import UTC, parse_datetime

#: Bumped when the shape changes in a way an older build cannot read.
SESSION_VERSION = 1

#: The default name, beside the workbook -- a session is the user's data.
SESSION_FILENAME = "vol_session.json"

#: What every block of a pair may carry.  Listed once so the reader, the
#: writer and the tests all agree on the vocabulary.
PAIR_BLOCKS = ("curve", "events", "atm_overwrites", "smile_overwrites",
               "param_shifts", "anchor_tenors", "band")

#: The suffix a workbook copy gets when a session is exported without a
#: named destination: ``vol_marks.xlsx`` -> ``vol_marks_marked.xlsx``.
EXPORT_SUFFIX = "_marked"

#: How the workbook spells each curve parameter's PARAMS row, for a row the
#: sheet does not have yet.  The reader accepts these (``marketdata.PARAM_ROWS``).
_CURVE_ROW_LABEL = {
    "initial_vol": "initial", "long_term_vol": "long term", "mean_reversion": "MR",
    "short_addon": "addon", "short_decay": "short decay", "rate_vol": "ratevol",
    "rate_corr": "rate corr",
    # a cross: the same three cells mean the correlation term structure
    "corr_initial": "initial", "corr_final": "long term", "corr_decay": "MR",
}

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
        # The parts and the total, in points: the total is what an old file
        # holds and what a reader wants, the parts are what was marked.
        "events": [{"when": e.when.strftime("%Y-%m-%dT%H:%M"),
                    "bump": e.bump * 100.0,
                    "weights": {c: v * 100.0 for c, v in e.weights.items()},
                    "adjust": (e.adjust or 0.0) * 100.0,
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
        # The event weight table is the book's, not a pair's: what each
        # release is worth on each currency, in points.  Saved whole.
        "event_weights": book.econ.table(),
    }


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------
def apply_block(surface, block: dict) -> list[str]:
    """One pair's marks, in the order the screen would make them.

    Also what the book calls for the marks a workbook holds in its own
    session rows (``marketdata.overlay_label``), which are read into exactly
    this shape: one reader of the vocabulary, wherever the block came from.

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
        rows = [r if isinstance(r, dict) else {} for r in (block.get("events") or [])]
        entries, bad = event_entries(rows)
        problems.extend(bad)
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
            problems.extend(f"{pair}: {x}" for x in apply_block(book[pair], block))
        except Exception as exc:  # noqa: BLE001 - one pair, not the whole file
            problems.append(f"{pair}: {type(exc).__name__}: {exc}")
            continue
        applied.append(pair)

    if "event_weights" in doc:
        # Replace, like everything else here.  The weights feed Auto-load
        # only; events already on a pair keep the parts they were given.
        try:
            book.econ.set_weights(doc.get("event_weights") or {})
        except ValueError as exc:
            problems.append(f"event weights: {exc}")

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


# --------------------------------------------------------------------------
# export: a session file into a workbook
# --------------------------------------------------------------------------
def export_workbook(doc: dict, workbook: str | Path, out: str | Path | None = None,
                    pairs=None, *, in_place: bool = False) -> dict:
    """Write a session's marks into the cells a workbook keeps them in.

    The workbook is the book of record and nothing else in this package
    writes to it (§2).  This is the one deliberate exception, and it is an
    *export*: it is never run by a screen on its own, it writes a **copy**
    beside the original (``<name>_marked.xlsx``) unless ``in_place`` says
    otherwise, and it says what it wrote and what it could not.

    What goes where.  The curve parameters go into the PARAMS rows the
    reader already reads (a cross's ``corr_*`` into the same three cells the
    workbook has always used for a correlation).  Events go onto PARAMS
    event rows in the workbook's own clock (Hong Kong, ``event_tz_offset``
    hours ahead of the file's UTC), weights into the currency columns and
    the pair's adjustment into its own column; a row or a currency column
    the sheet lacks is added.  The marks the workbook had no cell for -- ATM
    and smile overwrites, wing shifts, the anchor switch -- go into rows
    ``marketdata.overlay_label`` reads (``atm 1m``, ``slog25 3m``,
    ``shift rho25``, ``anchor``), and the band treatment into a ``BANDS``
    sheet.  Every one of them is read back by ``ExcelSource``, so a workbook
    written here loads as the session it came from; writing a cell the tool
    would not read is the silent zero this project exists to remove.

    A pair is replaced, never merged: its events, overwrites and shifts in
    the file are the whole of what the workbook then holds for it.  The one
    thing that cannot be replaced per pair is an event's *currency* weights,
    which every pair with that currency shares -- the file's are written and
    none are removed, and a weight two pairs in the file disagree on is
    reported.

    Formulas and the other sheets are kept as they are; images and charts,
    if any, are not (openpyxl does not carry them), which is the other reason
    the default is a copy.  Returns ``{"written", "pairs", "problems",
    "notes"}``.
    """
    import openpyxl
    from datetime import timedelta
    from .marketdata import BANDS_SHEET, PARAM_ROWS, _norm, overlay_label
    from .surface import PARAM_NAMES as _SMILE

    if not isinstance(doc, dict) or not isinstance(doc.get("pairs"), dict):
        raise SessionError("that is not a volkit session file: it has no 'pairs' object")
    src = Path(workbook)
    if not src.exists():
        raise SessionError(f"workbook not found: {src}")
    dst = Path(out) if out else src.with_name(src.stem + EXPORT_SUFFIX + src.suffix)
    if dst.resolve() == src.resolve() and not in_place:
        raise SessionError(
            f"{src.name} is the workbook itself; writing into it needs in_place "
            "(--in-place), or name another file")
    if dst.suffix.lower() != ".xlsx":
        raise SessionError(f"{dst.name}: a workbook is written as .xlsx")

    # Read whole and closed before parsing, like every workbook here
    # (``marketdata.open_workbook``): an .xlsx held open is one Excel cannot
    # save.
    import io
    with src.open("rb") as fh:
        blob = fh.read()
    wb = openpyxl.load_workbook(io.BytesIO(blob))
    # openpyxl keeps a formula and drops the value Excel last computed for
    # it, and pandas reads a formula with no cached value as blank -- so a
    # copy saved by openpyxl alone has smile sheets full of ``=C2*3`` that
    # read as no quote at all until Excel has opened and saved it.  The
    # cached values are read here and put back into the saved file.
    cached = _formula_cache(wb, openpyxl.load_workbook(io.BytesIO(blob), data_only=True))
    if "PARAMS" not in wb.sheetnames:
        raise SessionError(f"{src.name} has no PARAMS sheet")
    ws = wb["PARAMS"]
    tz_shift = timedelta(hours=8.0)  # ExcelSource's default event_tz_offset_hours

    problems: list[str] = []
    notes: list[str] = []
    written: list[str] = []

    # -- the sheet as it stands -------------------------------------------
    header = {}
    for c in range(2, ws.max_column + 1):
        v = ws.cell(row=1, column=c).value
        if v is not None and str(v).strip():
            header[str(v).strip().upper()] = c
    ncol = [ws.max_column]

    def column_for(name: str, *, create: bool) -> int | None:
        key = name.upper()
        if key in header:
            return header[key]
        if not create:
            return None
        ncol[0] += 1
        ws.cell(row=1, column=ncol[0], value=key)
        header[key] = ncol[0]
        return ncol[0]

    rows_by_key: dict[str, int] = {}
    event_rows: dict[datetime, int] = {}
    overlay_rows: dict[tuple, int] = {}
    # Rows are appended after the last *labelled* one: ``max_row`` counts
    # formatted-but-empty rows, and a gap of blank labels is what the reader
    # then meets as a row that is neither a parameter nor a date.
    nrow = [1]
    for r in range(2, ws.max_row + 1):
        label = ws.cell(row=r, column=1).value
        if label is None or not str(label).strip():
            continue
        nrow[0] = r
        key = PARAM_ROWS.get(_norm(label))
        if key is not None:
            rows_by_key[key] = r
            continue
        when = None
        if isinstance(label, datetime):
            when = label.replace(tzinfo=None, second=0, microsecond=0)
        else:
            try:
                when = parse_datetime(str(label)).replace(tzinfo=None, second=0, microsecond=0)
            except ValueError:
                when = None
        if when is not None:
            event_rows[when] = r
            continue
        ov = overlay_label(label)
        if ov is not None:
            overlay_rows[ov] = r

    def row_for(key: str, label: str) -> int:
        if key in rows_by_key:
            return rows_by_key[key]
        nrow[0] += 1
        ws.cell(row=nrow[0], column=1, value=label)
        rows_by_key[key] = nrow[0]
        return nrow[0]

    def overlay_row(ov: tuple) -> int:
        if ov in overlay_rows:
            return overlay_rows[ov]
        nrow[0] += 1
        # the labels ``marketdata.overlay_label`` reads: a smile overwrite is
        # spelled by its parameter and tenor alone
        ws.cell(row=nrow[0], column=1, value=" ".join(ov[1:] if ov[0] == "smile" else ov))
        overlay_rows[ov] = nrow[0]
        return nrow[0]

    def event_row(when_local: datetime) -> int:
        if when_local in event_rows:
            return event_rows[when_local]
        nrow[0] += 1
        cell = ws.cell(row=nrow[0], column=1, value=when_local)
        cell.number_format = "yyyy-mm-dd hh:mm"
        event_rows[when_local] = nrow[0]
        return nrow[0]

    def clear(rows, col: int) -> None:
        for r in rows:
            ws.cell(row=r, column=col, value=None)

    def number(what: str, v):
        try:
            return float(v)
        except (TypeError, ValueError):
            problems.append(f"{what}: {v!r} is not a number")
            return None

    wanted = {k.upper() for k in doc["pairs"]} if pairs is None else \
        {str(x).upper() for x in pairs}
    weight_cells: dict[tuple[int, int], tuple[float, str]] = {}
    #: pair -> column, and the event rows its own schedule named
    scheduled: dict[str, tuple[int, set[int]]] = {}

    for raw_name, block in doc["pairs"].items():
        name = str(raw_name).upper()
        if name not in wanted:
            continue
        if not isinstance(block, dict):
            problems.append(f"{name}: its entry in the file is not an object")
            continue
        col = column_for(name, create=False)
        if col is None:
            problems.append(f"{name}: the workbook has no PARAMS column for it (list the pair "
                            "in CONFIG and give it a PARAMS column first); not written")
            continue
        wrote: list[str] = []

        # curve -----------------------------------------------------------
        curve = block.get("curve")
        if isinstance(curve, dict):
            for k, v in curve.items():
                if v in (None, ""):
                    continue
                label = _CURVE_ROW_LABEL.get(k)
                if label is None:
                    problems.append(f"{name}: curve parameter {k!r} is not one the workbook holds")
                    continue
                fv = number(f"{name} curve {k}", v)
                if fv is None:
                    continue
                key = PARAM_ROWS[_norm(label)]
                ws.cell(row=row_for(key, label), column=col, value=fv)
            wrote.append("curve")

        # events ----------------------------------------------------------
        if "events" in block:
            rows = [r if isinstance(r, dict) else {} for r in (block.get("events") or [])]
            entries, bad = event_entries(rows)
            problems.extend(f"{name}: {b}" for b in bad)
            clear(event_rows.values(), col)
            named: set[int] = set()
            for e in entries:
                local = (e.when.astimezone(UTC) + tz_shift).replace(tzinfo=None, second=0,
                                                                   microsecond=0)
                r = event_row(local)
                named.add(r)
                if any(e.weights.values()):
                    for ccy, w in e.weights.items():
                        if not w:
                            continue
                        wcol = column_for(ccy, create=True)
                        prior = weight_cells.get((r, wcol))
                        if prior is not None and abs(prior[0] - w * 100.0) > 1e-9:
                            problems.append(
                                f"{name}: event {e.when:%Y-%m-%d %H:%M}Z weight {ccy} "
                                f"{w * 100:.4g} disagrees with {prior[1]}'s {prior[0]:.4g}; "
                                f"{name}'s was written last")
                        ws.cell(row=r, column=wcol, value=w * 100.0)
                        weight_cells[(r, wcol)] = (w * 100.0, name)
                    ws.cell(row=r, column=col, value=(e.adjust or 0.0) * 100.0)
                else:
                    ws.cell(row=r, column=col, value=(e.bump or 0.0) * 100.0)
            scheduled[name] = (col, named)
            wrote.append(f"{len(entries)} event(s)")

        # overwrites, shifts, anchor ---------------------------------------
        if "atm_overwrites" in block:
            clear((r for ov, r in overlay_rows.items() if ov[0] == "atm"), col)
            for tenor, v in (block.get("atm_overwrites") or {}).items():
                fv = number(f"{name} ATM overwrite {tenor}", v)
                if fv is not None:
                    ws.cell(row=overlay_row(("atm", str(tenor).lower())), column=col, value=fv)
            wrote.append(f"{len(block.get('atm_overwrites') or {})} ATM overwrite(s)")
        if "smile_overwrites" in block:
            clear((r for ov, r in overlay_rows.items() if ov[0] == "smile"), col)
            n = 0
            for pname, tenors in (block.get("smile_overwrites") or {}).items():
                if pname not in _SMILE:
                    problems.append(f"{name}: smile overwrite {pname!r} is not a smile parameter")
                    continue
                for tenor, v in (tenors or {}).items():
                    fv = number(f"{name} smile overwrite {pname} {tenor}", v)
                    if fv is not None:
                        ws.cell(row=overlay_row(("smile", pname, str(tenor).lower())),
                                column=col, value=fv)
                        n += 1
            wrote.append(f"{n} smile overwrite(s)")
        if "param_shifts" in block:
            clear((r for ov, r in overlay_rows.items() if ov[0] == "shift"), col)
            n = 0
            for pname, v in (block.get("param_shifts") or {}).items():
                if pname not in _SMILE:
                    problems.append(f"{name}: wing shift {pname!r} is not a smile parameter")
                    continue
                fv = number(f"{name} wing shift {pname}", v)
                if fv:
                    ws.cell(row=overlay_row(("shift", pname)), column=col, value=fv)
                    n += 1
            wrote.append(f"{n} wing shift(s)")
        if "anchor_tenors" in block:
            ws.cell(row=overlay_row(("anchor",)), column=col,
                    value=1 if block["anchor_tenors"] else None)
            if block["anchor_tenors"]:
                wrote.append("anchored")

        # band ------------------------------------------------------------
        if isinstance(block.get("band"), dict):
            try:
                treatment = BandTreatment.from_request(block["band"])
            except (TypeError, ValueError) as exc:
                problems.append(f"{name}: band treatment: {exc}")
            else:
                _write_band_row(wb, BANDS_SHEET, name, treatment.to_request())
                wrote.append(f"band {treatment.mode}")

        written.append(name)
        notes.append(f"{name}: " + ", ".join(wrote))

    # A currency weight is shared by every pair with that currency, so a
    # pair whose saved schedule does *not* have an event that the sheet's
    # weights would give it takes it anyway on reload.  The file's schedule
    # is the whole schedule for that pair, so the pair's own cell cancels
    # the legs -- after every pair's weights are down, because a weight
    # written for the last pair reaches the first.  What is not done is
    # zeroing the weight: it belongs to the other pairs too.
    from .events import leg_weights, pair_bump
    ccy_cols = {k: c for k, c in header.items() if len(k) == 3 and k.isalpha()}
    for name, (col, named) in scheduled.items():
        for when_local, r in event_rows.items():
            if r in named:
                continue
            table = {}
            for ccy, c in ccy_cols.items():
                v = ws.cell(row=r, column=c).value
                if isinstance(v, (int, float)) and v:
                    table[ccy] = float(v)
            if not table:
                continue
            total = pair_bump(leg_weights(table, name), name, 0.0)
            if abs(total) < 1e-12:
                continue
            ws.cell(row=r, column=col, value=-total)
            notes.append(f"{name}: the file has no event at {when_local:%Y-%m-%d %H:%M} "
                         f"(sheet time) but the sheet's currency weights would give it "
                         f"{total:.4g}; its own cell cancels them, since the weights belong "
                         "to the other pairs too")
    if weight_cells:
        touched = sorted({ws.cell(row=1, column=c).value for (_, c) in weight_cells})
        notes.append(f"currency weight(s) written for {', '.join(touched)}: a weight is "
                     "shared, so a pair the file does not mention takes it too")

    if not written:
        problems.append("nothing was written")
    else:
        _save_workbook(wb, dst, cached)
        n = sum(len(v) for v in cached.values())
        if n:
            notes.append(f"{n} formula cell(s) kept, with the values Excel last computed "
                         "for them; a formula reading a cell written here shows its old "
                         "value until Excel recalculates")
    return {"written": str(dst) if written else "", "pairs": written,
            "problems": problems, "notes": notes, "in_place": bool(in_place)}


def _write_band_row(wb, sheet: str, pair: str, request: dict) -> None:
    """The pair's row on the BANDS sheet, replaced.  The header is the
    request's own keys, so the reader's column matching is against the same
    spelling ``to_request`` uses; a column the sheet already has keeps its
    place and any it lacks is appended."""
    if sheet in wb.sheetnames:
        ws = wb[sheet]
    else:
        ws = wb.create_sheet(sheet)
        ws.cell(row=1, column=1, value="pair")
    header = {}
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=1, column=c).value
        if v is not None and str(v).strip():
            header[str(v).strip().lower().replace(" ", "_")] = c
    if "pair" not in header:
        raise SessionError(f"sheet {sheet!r} has no 'pair' column; not touching it")
    row = None
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=header["pair"]).value
        if v is not None and str(v).strip().upper() == pair:
            row = r
            break
    if row is None:
        row = ws.max_row + 1 if ws.max_row > 1 or ws.cell(row=1, column=1).value else 2
        # a fresh sheet reports max_row 1; an emptied one may report more
        while ws.cell(row=row, column=header["pair"]).value not in (None, ""):
            row += 1
    ws.cell(row=row, column=header["pair"], value=pair)
    for key, value in request.items():
        if key not in header:
            c = ws.max_column + 1
            ws.cell(row=1, column=c, value=key)
            header[key] = c
        if isinstance(value, bool):
            value = 1 if value else 0
        ws.cell(row=row, column=header[key], value=value)


def _formula_cache(wb, values) -> dict[int, dict[str, float]]:
    """Every numeric value Excel last computed for a formula cell, by
    worksheet position and coordinate.  Only numbers: the sheets the tool
    reads are numeric, and a string result would need the shared-string
    table rewritten, which is not worth carrying for a label."""
    out: dict[int, dict[str, float]] = {}
    for i, ws in enumerate(wb.worksheets, start=1):
        vs = values[ws.title]
        for row in ws.iter_rows():
            for cell in row:
                v = cell.value
                if isinstance(v, str) and v.startswith("="):
                    cv = vs[cell.coordinate].value
                    if isinstance(cv, (int, float)) and not isinstance(cv, bool):
                        out.setdefault(i, {})[cell.coordinate] = float(cv)
    return out


_EMPTY_V = re.compile(r'<c r="([A-Z]+[0-9]+)"([^>]*)><f>([^<]*)</f><v\s*/></c>')


def _restore_formula_cache(xlsx: bytes, cached: dict[int, dict[str, float]]) -> bytes:
    """Put the cached values back into the file openpyxl wrote.

    openpyxl writes ``<c r="B2"><f>C2*3</f><v /></c>``, and its worksheet
    files are numbered in ``wb.worksheets`` order, so this is one
    substitution per formula cell whose value is known.  A cell the export
    wrote a number into is no longer a formula and is not touched here.
    """
    import io
    import zipfile
    if not cached:
        return xlsx
    src = zipfile.ZipFile(io.BytesIO(xlsx))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            m = re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", item.filename)
            if m and int(m.group(1)) in cached:
                values = cached[int(m.group(1))]

                def fill(match):
                    v = values.get(match.group(1))
                    if v is None:
                        return match.group(0)
                    return (f'<c r="{match.group(1)}"{match.group(2)}><f>{match.group(3)}</f>'
                            f'<v>{v!r}</v></c>')
                data = _EMPTY_V.sub(fill, data.decode("utf-8")).encode("utf-8")
            out.writestr(item, data)
    return buf.getvalue()


def _save_workbook(wb, dst: Path, cached: dict | None = None) -> None:
    """Atomic, like ``write``: a workbook is either the old one or the new
    one, never half of each."""
    import io
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".volkit-", suffix=".xlsx", dir=str(dst.parent))
    try:
        os.close(fd)
        buf = io.BytesIO()
        wb.save(buf)
        with open(tmp, "wb") as fh:
            fh.write(_restore_formula_cache(buf.getvalue(), cached or {}))
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
