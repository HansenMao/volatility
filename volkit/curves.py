"""Several volatility curves side by side, including the same curve on other dates.

The analysis screen answers "is this mark right"; this answers the question a
marker actually asks first, which is "what has changed".  A curve here is a
term structure of the five quoted numbers -- at-the-money, and the 25 and 10
delta risk reversal and butterfly -- and it can come from four places:

``surface``   the fitted surface as the book has it now, read at a cut.  This
              is what the pricing screen would return, smile and all.
``marks``     what the workbook quotes: the marked at-the-money term structure
              and the raw risk reversal / market strangle rows.  Against
              ``surface`` this is the fit residual, tenor by tenor.
``history``   one row of the historical workbook, chosen by date.  Several of
              these is the same curve on different days.
``paste``     a curve typed or pasted in -- a broker run, another system, last
              night's close out of a mail.

Two things are deliberate.

*A curve that cannot be built keeps its place and carries its reason*, and so
does a single tenor inside one.  A comparison table with a row quietly missing
reads as agreement; that is the failure this project exists to remove.

*The volatility unit of a paste is decided once, from its at-the-money column*,
and refused when the level quotes straddle 1.0 -- the same rule §9 gives a
historical sheet and §11 gives a broker run.  A 0.35 risk reversal is an
ordinary quote in points and an ordinary at-the-money in decimals, so letting
a wing vote returns it a hundred times too large.

Everything in and out of this module is in decimals; the volatility points a
human reads are converted at the edges, once, by the caller that prints them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .timeutil import TenorError, parse_datetime, tenor_to_years

#: Where a curve may come from.
CURVE_KINDS = ("surface", "marks", "history", "paste")

#: The five quoted numbers a curve carries, in the order they are displayed.
CURVE_FIELDS = ("atm", "rr25", "bf25", "rr10", "bf10")

FIELD_LABELS = {
    "atm": "at-the-money",
    "rr25": "25d risk reversal",
    "bf25": "25d butterfly",
    "rr10": "10d risk reversal",
    "bf10": "10d butterfly",
}

KIND_LABELS = {
    "surface": "fitted surface",
    "marks": "workbook quotes",
    "history": "historical workbook",
    "paste": "pasted curve",
}

#: How many curves one panel may hold.  Not a model limit -- a guard against a
#: stored panel with a runaway list re-fitting every smile in the book.
MAX_CURVES = 12

_OFFSET = re.compile(r"^-\s*(\d+(?:\.\d+)?)\s*([dwmy]?)$", re.I)
_OFFSET_UNITS = {"": 1.0, "d": 1.0, "w": 7.0, "m": 365.2425 / 12.0, "y": 365.2425}


class CurveError(ValueError):
    """A curve request that cannot be honoured, with the reason."""


# --------------------------------------------------------------------------
# one curve
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class CurveRequest:
    """One entry in the comparison panel, exactly as the browser posts it."""

    kind: str
    pair: str = ""
    date: str = ""
    label: str = ""
    text: str = ""
    on: bool = True

    def __post_init__(self) -> None:
        if self.kind not in CURVE_KINDS:
            raise CurveError(
                f"unknown curve source {self.kind!r}; expected one of {', '.join(CURVE_KINDS)}")


@dataclass
class CurvePoint:
    """One tenor of one curve.

    ``values`` holds every field this source could produce and ``None`` for
    the ones it could not; ``message`` says why, so a blank cell is never
    mistaken for a zero.
    """

    tenor: str
    t: float
    values: dict[str, float | None] = field(default_factory=dict)
    diffs: dict[str, float | None] = field(default_factory=dict)
    message: str = ""


@dataclass
class Curve:
    """One built curve, or one that could not be built and says why."""

    label: str
    kind: str
    pair: str
    source: str = ""
    asof: str = ""
    ok: bool = True
    message: str = ""
    is_base: bool = False
    points: list[CurvePoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def at(self, tenor: str) -> CurvePoint | None:
        for p in self.points:
            if p.tenor.upper() == tenor.upper():
                return p
        return None


def _point(tenor: str, values: dict[str, float | None], message: str = "") -> CurvePoint:
    """One point, carrying every field -- the ones it has not got as ``None``.

    Filled out rather than sparse, so a source that cannot produce a field is
    a blank cell in the same row as the others rather than a missing key the
    front end has to guess about.
    """
    full: dict[str, float | None] = {f: None for f in CURVE_FIELDS}
    full.update({k: v for k, v in values.items() if k in full})
    return CurvePoint(tenor=tenor, t=tenor_to_years(tenor), values=full, message=message)


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------
def surface_curve(book, pair: str, *, cut: str = "NY", method: str | None = None) -> Curve:
    """The fitted surface, read at every quoted tenor.

    Each tenor is read inside its own guard.  One expiry whose smile will not
    solve must not empty the curve -- it takes its own row out and leaves the
    rest of the term structure standing.
    """
    if pair not in book:
        raise CurveError(f"{pair} is not built in this book")
    surface = book[pair]
    curve = Curve(label="", kind="surface", pair=pair,
                  source=f"fitted surface, {method or surface.method} at the {cut} cut",
                  asof=book.clock.now.isoformat())
    for tenor in book.data.tenor_points:
        t = tenor_to_years(tenor)
        expiry = book.clock.datetime_from_years(t)
        values: dict[str, float | None] = {}
        why = ""
        try:
            values["atm"] = float(surface.atm.cut_vol(expiry, cut))
        except Exception as exc:  # noqa: BLE001 - one tenor, not the curve
            why = f"{type(exc).__name__}: {exc}"
        for d, tag in ((0.25, "25"), (0.10, "10")):
            try:
                values[f"rr{tag}"] = float(surface.risk_reversal(expiry, d, method, cut))
                values[f"bf{tag}"] = float(surface.strangle(expiry, d, method, cut))
            except Exception as exc:  # noqa: BLE001
                why = why or f"{tag}d wing: {type(exc).__name__}: {exc}"
        curve.points.append(_point(tenor, values, why))
    return curve


def marks_curve(book, pair: str) -> Curve:
    """What the workbook says: the marked at-the-money curve and the raw quotes.

    The butterfly here is the **market strangle** the sheet quotes, which is
    the same quantity :meth:`VolSurface.strangle` returns, so the two curves
    are comparable row for row.  Anything else would compare a market strangle
    against a smile fly and call the difference a marking error.
    """
    if pair not in book:
        raise CurveError(f"{pair} is not built in this book")
    surface = book[pair]
    marks = book.data.marks.get(pair) or []
    curve = Curve(label="", kind="marks", pair=pair,
                  source="workbook quotes: marked ATM curve, quoted RR and market strangle",
                  asof=book.clock.now.isoformat())
    if not marks:
        curve.ok = False
        curve.message = f"{pair} has no smile quotes in {book.data.source}"
        return curve
    for mark in marks:
        try:
            t = tenor_to_years(mark.tenor)
        except TenorError as exc:
            curve.warnings.append(str(exc))
            continue
        curve.points.append(_point(mark.tenor, {
            "atm": float(surface.atm.term_vol(t)),
            "rr25": float(mark.rr_25), "bf25": float(mark.st_25),
            "rr10": float(mark.rr_10), "bf10": float(mark.st_10),
        }))
    return curve


def resolve_history_date(hist, wanted: str) -> tuple[date, str]:
    """The row a date request lands on, and a sentence about how far it moved.

    Accepts a date, an empty string or ``latest`` for the last row, and a
    negative offset (``-30``, ``-30d``, ``-3m``) measured back from it.  The
    row chosen is the last one **on or before** the requested date: a
    historical workbook has no rows on weekends and holidays, and snapping
    forward would compare a Friday mark against the following Monday's.
    """
    if not hist.dates:
        raise CurveError(f"{hist.pair}: the historical sheet has no dated rows")
    last = hist.dates[-1]
    text = (wanted or "").strip()
    if not text or text.lower() in ("latest", "last", "now"):
        return last, "the most recent row"

    m = _OFFSET.match(text)
    if m:
        days = float(m.group(1)) * _OFFSET_UNITS[m.group(2).lower()]
        target = last - timedelta(days=days)
    else:
        try:
            target = parse_datetime(text).date()
        except Exception as exc:  # noqa: BLE001
            raise CurveError(
                f"{text!r} is not a date, 'latest', or an offset like '-30d': {exc}") from None

    earlier = [d for d in hist.dates if d <= target]
    if not earlier:
        first = hist.dates[0]
        return first, (f"asked for {target:%Y-%m-%d}, which is before the sheet starts; "
                       f"using its first row, {first:%Y-%m-%d}")
    got = earlier[-1]
    gap = (target - got).days
    if gap == 0:
        return got, f"{got:%Y-%m-%d}"
    return got, f"asked for {target:%Y-%m-%d}; nearest row on or before it is {got:%Y-%m-%d}"


def history_curve(hist, wanted: str = "") -> Curve:
    """One dated row of a historical sheet, as a curve."""
    when, note = resolve_history_date(hist, wanted)
    i = hist.dates.index(when)
    curve = Curve(label="", kind="history", pair=hist.pair,
                  source=f"historical workbook, {note}", asof=when.isoformat())
    for tenor in hist.tenors:
        values: dict[str, float | None] = {}
        for name, series in (("atm", hist.series("atm", tenor)),
                             ("rr25", hist.series("rr", tenor, 25)),
                             ("bf25", hist.series("bf", tenor, 25)),
                             ("rr10", hist.series("rr", tenor, 10)),
                             ("bf10", hist.series("bf", tenor, 10))):
            if series is None or i >= series.size:
                continue
            v = float(series[i])
            values[name] = None if v != v else v
        if not any(v is not None for v in values.values()):
            curve.points.append(_point(tenor, values, "no quote on this row"))
        else:
            curve.points.append(_point(tenor, values))
    if not curve.points:
        curve.ok = False
        curve.message = f"{hist.pair}: nothing readable at {when:%Y-%m-%d}"
    return curve


def parse_pasted_curve(text: str, *, label: str = "pasted curve") -> Curve:
    """Read ``tenor atm [rr25 bf25 rr10 bf10]`` lines.

    The unit is decided **once**, from the at-the-money column of the whole
    paste, and applied to the wings.  Deciding per column reads a small risk
    reversal as a decimal and returns it a hundred times too large; deciding
    per line does the same thing one row at a time.  A paste whose levels
    straddle 1.0 is refused rather than guessed.
    """
    rows: list[tuple[int, str, list[float]]] = []
    bad: list[str] = []
    for n, line in enumerate(text.splitlines(), start=1):
        body = line.split("#", 1)[0].replace(",", " ").replace("\t", " ").strip()
        if not body:
            continue
        bits = body.split()
        if len(bits) < 2:
            bad.append(f"line {n}: expected a tenor and at least an at-the-money level")
            continue
        try:
            tenor_to_years(bits[0])
        except TenorError as exc:
            bad.append(f"line {n}: {exc}")
            continue
        try:
            nums = [float(x) for x in bits[1:6]]
        except ValueError:
            bad.append(f"line {n}: {' '.join(bits[1:6])!r} is not a row of numbers")
            continue
        rows.append((n, bits[0].upper(), nums))

    curve = Curve(label=label, kind="paste", pair="", source="pasted curve")
    if bad:
        curve.ok = False
        curve.message = "the pasted curve has bad lines: " + "; ".join(bad)
        return curve
    if not rows:
        curve.ok = False
        curve.message = ("nothing to read: one line per tenor, "
                         "'1M 8.20' or '1M 8.20 -0.35 0.22 -0.60 0.75'")
        return curve

    levels = [nums[0] for _, _, nums in rows]
    lo, hi = min(levels), max(levels)
    if lo < 1.0 <= hi:
        curve.ok = False
        curve.message = (f"the at-the-money levels straddle 1.0 ({lo:g} to {hi:g}), so the "
                         "paste could be in volatility points or in decimals; it is refused "
                         "rather than guessed")
        return curve
    divisor, unit = (100.0, "volatility points") if hi >= 1.0 else (1.0, "decimals")
    curve.source = f"pasted curve, {len(rows)} tenor(s), read as {unit}"
    for _, tenor, nums in rows:
        values = {name: nums[k] / divisor
                  for k, name in enumerate(CURVE_FIELDS) if k < len(nums)}
        curve.points.append(_point(tenor, values))
    curve.points.sort(key=lambda p: p.t)
    return curve


def build_curve(req: CurveRequest, book, history=None, *, cut: str = "NY",
                method: str = "SVI") -> Curve:
    """One curve from one request, whichever of the four sources it names.

    The comparison panel and the monitor screen both need exactly this, and a
    second copy of the dispatch would be a second place for a source to be
    added to only one of them.
    """
    if req.kind == "paste":
        return parse_pasted_curve(req.text, label=req.label or "pasted curve")
    if req.kind == "history":
        if history is None:
            raise CurveError("no historical workbook is loaded, so no dated curve can be read")
        if req.pair not in history:
            raise CurveError(
                f"the historical workbook has no sheet for {req.pair}; it holds "
                f"{', '.join(sorted(history.pairs)) or 'nothing readable'}")
        return history_curve(history[req.pair], req.date)
    if book is None:
        raise CurveError("no workbook is loaded")
    if req.kind == "surface":
        return surface_curve(book, req.pair, cut=cut, method=method)
    return marks_curve(book, req.pair)


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ComparePanel:
    """The comparison panel, owned by the browser and posted whole.

    The server keeps none of it, for the same reason the listed and
    market-maker panels are posted whole: ``volkit analysis --compare`` then
    reproduces a screen exactly, and every endpoint stays a pure function of
    its request plus the book.
    """

    curves: tuple[CurveRequest, ...] = ()
    cut: str = "NY"
    method: str = "SVI"
    field: str = "atm"
    base: int = 0

    def __post_init__(self) -> None:
        if self.field not in CURVE_FIELDS:
            raise CurveError(
                f"unknown field {self.field!r}; expected one of {', '.join(CURVE_FIELDS)}")
        if len(self.curves) > MAX_CURVES:
            raise CurveError(
                f"{len(self.curves)} curves were requested; a panel holds at most {MAX_CURVES}")

    # -- building ---------------------------------------------------------
    def _build_one(self, req: CurveRequest, book, history) -> Curve:
        return build_curve(req, book, history, cut=self.cut, method=self.method)

    def default_label(self, req: CurveRequest, curve: Curve) -> str:
        if req.label.strip():
            return req.label.strip()
        if req.kind == "history":
            return f"{curve.pair or req.pair} {curve.asof or req.date or 'latest'}"
        if req.kind == "paste":
            return "pasted"
        return f"{req.pair} {'quotes' if req.kind == 'marks' else 'surface'}"

    def run(self, book, history=None) -> dict:
        """Build every curve, difference them against the base, and report.

        A curve that fails is *kept*, marked not-ok and carrying its message.
        Dropping it would make a short comparison look complete.
        """
        wanted = [r for r in self.curves if r.on]
        built: list[Curve] = []
        for req in wanted:
            try:
                curve = self._build_one(req, book, history)
            except Exception as exc:  # noqa: BLE001 - reported in the row
                curve = Curve(label="", kind=req.kind, pair=req.pair, ok=False,
                              message=f"{type(exc).__name__}: {exc}"
                                      if not isinstance(exc, CurveError) else str(exc))
            curve.label = self.default_label(req, curve)
            built.append(curve)

        live = [c for c in built if c.ok and c.points]
        base_index = self.base if 0 <= self.base < len(built) else 0
        base = built[base_index] if built else None
        if base is not None and not (base.ok and base.points) and live:
            # The chosen base could not be built.  Fall back to the first one
            # that could, and say so, rather than reporting every difference
            # as unavailable.
            base = live[0]
            base_index = built.index(base)
        for i, c in enumerate(built):
            c.is_base = (i == base_index)

        # The book quotes its tenor points in lower case and the sheets in
        # upper; the union is displayed in one case so the same tenor does not
        # appear twice on the axis.  Lookups are case-insensitive either way.
        tenors: list[str] = []
        for c in built:
            for p in c.points:
                if p.tenor.upper() not in tenors:
                    tenors.append(p.tenor.upper())
        tenors.sort(key=tenor_to_years)

        for c in built:
            for p in c.points:
                if base is None or c is base:
                    p.diffs = {f: None for f in CURVE_FIELDS}
                    continue
                bp = base.at(p.tenor)
                p.diffs = {
                    f: (None if bp is None or bp.values.get(f) is None or p.values.get(f) is None
                        else p.values[f] - bp.values[f])
                    for f in CURVE_FIELDS
                }

        notes: list[str] = []
        missing = [c.label for c in built
                   if c.ok and c.points and any(c.at(x) is None for x in tenors)]
        if missing:
            notes.append("not every curve quotes every tenor; a blank cell is a tenor that "
                         f"source does not have ({', '.join(missing)})")
        if any(c.kind == "history" for c in built) and any(c.kind in ("marks", "surface")
                                                           for c in built):
            notes.append("a historical sheet's butterfly column is whatever that desk quoted; "
                         "the book's is a market strangle. Check the two mean the same thing "
                         "before reading a difference off the fly rows")

        return {
            "cut": self.cut, "method": self.method, "field": self.field,
            "fields": [{"key": f, "label": FIELD_LABELS[f]} for f in CURVE_FIELDS],
            "tenors": tenors,
            "base": base_index,
            "base_label": base.label if base is not None else "",
            "notes": notes,
            "curves": [{
                "label": c.label, "kind": c.kind, "kind_label": KIND_LABELS[c.kind],
                "pair": c.pair, "source": c.source, "asof": c.asof,
                "ok": c.ok, "message": c.message, "is_base": c.is_base,
                "warnings": list(c.warnings),
                "points": [{"tenor": p.tenor, "t": p.t, "values": p.values,
                            "diffs": p.diffs, "message": p.message} for p in c.points],
            } for c in built],
        }


def panel_from_request(payload: dict | None) -> ComparePanel:
    """Read the panel the browser posted.

    Every field the page sends is read here; a test pins that, because a
    setting the server ignores is one that silently does nothing.
    """
    payload = payload or {}
    raw = payload.get("curves") or []
    if not isinstance(raw, list):
        raise CurveError("'curves' must be a list of curve requests")
    reqs = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise CurveError(f"curve {i} is not an object")
        reqs.append(CurveRequest(
            kind=str(item.get("kind") or "surface").strip().lower(),
            pair=str(item.get("pair") or "").strip().upper(),
            date=str(item.get("date") or "").strip(),
            label=str(item.get("label") or ""),
            text=str(item.get("text") or ""),
            on=bool(item.get("on", True)),
        ))
    try:
        base = int(payload.get("base") or 0)
    except (TypeError, ValueError):
        base = 0
    return ComparePanel(
        curves=tuple(reqs),
        cut=str(payload.get("cut") or "NY"),
        method=str(payload.get("method") or "SVI"),
        field=str(payload.get("field") or "atm").strip().lower(),
        base=base,
    )


def parse_spec(text: str, default_pair: str = "") -> CurveRequest:
    """One ``--compare`` argument: ``kind[:date][:pair]``.

    ``surface``, ``marks``, ``history``, ``history:2024-01-15``,
    ``history:-30d:EURUSD``.  The pair defaults to the one being analysed, so
    the common case -- the same pair on several dates -- stays short.
    """
    bits = [b.strip() for b in str(text).split(":")]
    kind = (bits[0] or "surface").lower()
    if kind not in CURVE_KINDS or kind == "paste":
        raise CurveError(
            f"unknown comparison source {bits[0]!r}; expected one of "
            f"{', '.join(k for k in CURVE_KINDS if k != 'paste')}, optionally with "
            "':date' and ':pair', e.g. history:-30d:EURUSD")
    when = bits[1] if len(bits) > 1 else ""
    pair = (bits[2] if len(bits) > 2 else "") or default_pair
    if kind != "history" and when:
        # A date on a source that has none would otherwise be read, ignored
        # and reported as the live curve under a date the user chose.
        raise CurveError(f"{kind!r} is the curve as it stands now and takes no date; "
                         f"'{text}' asks for {when!r}")
    return CurveRequest(kind=kind, pair=pair.upper(), date=when)
