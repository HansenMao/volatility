"""Small panels that answer one question each: what has moved, and by how much.

The analysis screen asks whether a mark is right and the marking screen asks
what it is.  This one asks the question a desk asks before either of those,
first thing and then all day: *what is different from yesterday*.

A **tile** is one pair, two points in time, and the five quoted numbers
between them -- at-the-money, both risk reversals, both butterflies -- tenor by
tenor.  Either end is any of the sources ``curves.py`` already knows how to
build, so a tile can be the fitted surface against last week's close, the
workbook quotes against the surface fitted from them, or two dated rows of the
historical workbook against each other.  Nothing here builds a curve itself;
that would be a second place for a source to be added to only one screen.

Five things are decided here.

**A tile is a difference, and the difference is what it reports.**  The levels
at both ends are carried too, because a change of half a point means different
things at 6 and at 26, but the tile exists to show the change.

**Both ends are built independently and a broken one does not empty the tile.**
A tile whose "was" curve cannot be built still shows the "now" levels and says
what it could not difference them against.  A tenor one end does not quote is
a blank change, not an absent row.

**The panel is the browser's and is posted whole**, like the listed,
market-maker and comparison panels, so ``volkit monitor`` reproduces a screen
exactly and this stays a pure function of its request plus the book.

**A tile that has nothing to say says so.**  When both ends land on the same
day -- which is what a dated request does when the historical sheet has not
been updated -- the tile reports it rather than showing a column of zeros that
looks like a quiet market.

**A big move is marked, and the size of one is a number somebody chose.**  A
screen of sixteen tiles is a few hundred numbers and the eye has to find the
handful that matter.  Each change is graded against one threshold in
volatility points -- ``big`` -- and *every* field is graded on it, not only
the one the screen has highlighted: what has moved may not be what was being
watched.  The grade is decided here rather than in the browser so that the
screen and ``volkit monitor`` mark the same cells, and it is a **grade, not a
filter**: nothing is hidden, dropped or reordered by it, and a tile counts its
big moves in its own heading so a panel scrolled past still says how much is
in it.

Everything here is in decimals, like ``curves.py``; the volatility points a
human reads are converted once, by the screen or the command line that prints
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite

from .curves import (CURVE_FIELDS, CURVE_KINDS, FIELD_LABELS, KIND_LABELS,
                     CurveError, CurveRequest, build_curve)
from .timeutil import tenor_to_years

#: How many tiles one screen may hold.  Not a model limit -- a guard against a
#: stored panel with a runaway list re-fitting every smile in the book on
#: every refresh.
MAX_TILES = 16

#: The default comparison: the surface as it stands, against a week ago.
DEFAULT_NOW_KIND = "surface"
DEFAULT_WAS_KIND = "history"
DEFAULT_WAS_DATE = "-1w"

#: What counts as a big move, in **volatility points**, until somebody says
#: otherwise.  A quarter of a point is roughly where an overnight change in a
#: G10 at-the-money stops being noise and starts being something to look at;
#: it is a default and not a model constant, which is why the screen and the
#: command line both let it be typed.  One threshold serves all five numbers:
#: a risk reversal that has moved a quarter of a point has moved a great deal
#: more, in its own terms, than an at-the-money that has, and that is the
#: reading a desk wants rather than five separately tuned thresholds nobody
#: can hold in their head.
DEFAULT_BIG_MOVE = 0.25

#: A change at least this many times the threshold is graded 2 rather than 1.
#: Two tiers, because three shades of one colour stop being a signal.
BIG_MOVE_STEP = 2.0


def move_grade(change, big: float) -> int:
    """How big a change is: 0 (ordinary), 1 (big), 2 (very big).

    ``change`` and ``big`` are both in decimals, like everything else in this
    module.  A threshold of zero grades nothing -- that is how the marking is
    turned off, and zero means *off* rather than *mark everything*, because a
    screen where every cell is marked is a screen with nothing marked.
    """
    if change is None or big <= 0.0:
        return 0
    size = abs(change)
    if size >= big * BIG_MOVE_STEP:
        return 2
    return 1 if size >= big else 0


@dataclass(frozen=True)
class Tile:
    """One small panel: a pair, and the two ends of a comparison.

    ``now`` and ``was`` are named for what they usually are rather than for
    what they must be -- nothing stops the "was" end being the later of the
    two, and a tile that is asked for one reports the dates it landed on so
    the reader can see which way round it came out.
    """

    pair: str = ""
    now_kind: str = DEFAULT_NOW_KIND
    now_date: str = ""
    was_kind: str = DEFAULT_WAS_KIND
    was_date: str = DEFAULT_WAS_DATE
    label: str = ""
    on: bool = True

    def __post_init__(self) -> None:
        for name, kind in (("now", self.now_kind), ("was", self.was_kind)):
            if kind not in CURVE_KINDS or kind == "paste":
                raise CurveError(
                    f"the {name} end of a monitor tile cannot come from {kind!r}; expected "
                    f"one of {', '.join(k for k in CURVE_KINDS if k != 'paste')}")
        if not self.pair.strip():
            raise CurveError("a monitor tile needs a currency pair")

    def requests(self) -> tuple[CurveRequest, CurveRequest]:
        return (CurveRequest(kind=self.was_kind, pair=self.pair, date=self.was_date),
                CurveRequest(kind=self.now_kind, pair=self.pair, date=self.now_date))

    def default_label(self) -> str:
        if self.label.strip():
            return self.label.strip()
        def end(kind: str, when: str) -> str:
            if kind != "history":
                return KIND_LABELS[kind]
            return when.strip() or "latest"
        return f"{self.pair} {end(self.was_kind, self.was_date)} → {end(self.now_kind, self.now_date)}"


@dataclass
class TileResult:
    """One built tile, or one that could not be built and says why."""

    label: str
    pair: str
    ok: bool = True
    message: str = ""
    now: dict = field(default_factory=dict)
    was: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: How many graded cells this tile holds, and how many of those are on the
    #: second tier.  Counted here so a tile that is scrolled past, or whose
    #: table the reader is not looking at, still says in its heading how much
    #: has moved in it.
    moved: int = 0
    moved_hard: int = 0

    def as_dict(self) -> dict:
        return {"label": self.label, "pair": self.pair, "ok": self.ok,
                "message": self.message, "now": self.now, "was": self.was,
                "rows": self.rows, "warnings": list(self.warnings),
                "notes": list(self.notes),
                "moved": self.moved, "moved_hard": self.moved_hard}


def _end(curve) -> dict:
    return {"kind": curve.kind, "kind_label": KIND_LABELS[curve.kind],
            "source": curve.source, "asof": curve.asof, "ok": curve.ok,
            "message": curve.message}


def run_tile(tile: Tile, book, history=None, *, cut: str = "NY",
             method: str = "SVI", big: float = DEFAULT_BIG_MOVE / 100.0) -> TileResult:
    """Build both ends of one tile, difference them, and grade the differences.

    ``big`` is in decimals, like every other number here; the volatility
    points somebody types are converted at the edge that read them.
    """
    was_req, now_req = tile.requests()
    built = []
    for req in (was_req, now_req):
        try:
            built.append(build_curve(req, book, history, cut=cut, method=method))
        except Exception as exc:  # noqa: BLE001 - one end, not the tile
            from .curves import Curve
            built.append(Curve(label="", kind=req.kind, pair=req.pair, ok=False,
                               message=str(exc) if isinstance(exc, CurveError)
                               else f"{type(exc).__name__}: {exc}"))
    was, now = built
    out = TileResult(label=tile.default_label(), pair=tile.pair,
                     now=_end(now), was=_end(was),
                     warnings=list(now.warnings) + list(was.warnings))

    if not now.ok and not was.ok:
        out.ok = False
        out.message = f"neither end could be built: {now.message or was.message}"
        return out
    if not now.ok:
        out.notes.append(f"the current end could not be built ({now.message}), so this tile "
                         f"shows the earlier levels only")
    if not was.ok:
        out.notes.append(f"the earlier end could not be built ({was.message}), so this tile "
                         f"shows levels and no change")
    if (now.ok and was.ok and now.kind == "history" == was.kind
            and now.asof and now.asof == was.asof):
        # Two dated requests that landed on the same row.  Every change is
        # then zero, and a column of zeros reads as a quiet market rather than
        # as a comparison that never happened.  Only checked for two dated
        # sources: the surface and the workbook quotes are both stamped with
        # the valuation time and are a perfectly good comparison against each
        # other.
        out.notes.append(f"both ends landed on the same row ({now.asof[:10]}), so every change "
                         f"here is zero by construction, not because nothing moved")

    tenors: list[str] = []
    for c in (now, was):
        for p in c.points:
            if p.tenor.upper() not in tenors:
                tenors.append(p.tenor.upper())
    tenors.sort(key=tenor_to_years)

    for tenor in tenors:
        np_, wp = now.at(tenor), was.at(tenor)
        # The point's own place on the axis, which for a curve with a pair
        # behind it is the tenor's calendar expiry and not its nominal
        # length.  Two rows of one table have to be on one axis.
        t = (np_.t if np_ is not None else
             wp.t if wp is not None else tenor_to_years(tenor))
        row = {
            "tenor": tenor, "t": t,
            "now": {f: (None if np_ is None else np_.values.get(f)) for f in CURVE_FIELDS},
            "was": {f: (None if wp is None else wp.values.get(f)) for f in CURVE_FIELDS},
            "change": {}, "grade": {},
            "message": "; ".join(x for x in ((np_.message if np_ else ""),
                                             (wp.message if wp else "")) if x),
        }
        for f in CURVE_FIELDS:
            a, b = row["now"][f], row["was"][f]
            row["change"][f] = None if a is None or b is None else a - b
            # Every field is graded, not only the one the screen highlights:
            # what has moved may not be what was being watched.
            g = move_grade(row["change"][f], big)
            row["grade"][f] = g
            if g:
                out.moved += 1
                out.moved_hard += int(g > 1)
        if np_ is None:
            row["message"] = (row["message"] + "; " if row["message"] else "") + \
                "the current curve does not quote this tenor"
        if wp is None:
            row["message"] = (row["message"] + "; " if row["message"] else "") + \
                "the earlier curve does not quote this tenor"
        out.rows.append(row)

    if not out.rows:
        out.ok = False
        out.message = "neither end quoted a single tenor"
    return out


@dataclass(frozen=True)
class MonitorPanel:
    """Every tile on the screen, exactly as the browser posts it."""

    tiles: tuple[Tile, ...] = ()
    cut: str = "NY"
    method: str = "SVI"
    field: str = "atm"
    #: The big-move threshold, in decimals.  Zero turns the grading off.
    big: float = DEFAULT_BIG_MOVE / 100.0

    def __post_init__(self) -> None:
        if self.field not in CURVE_FIELDS:
            raise CurveError(
                f"unknown field {self.field!r}; expected one of {', '.join(CURVE_FIELDS)}")
        if len(self.tiles) > MAX_TILES:
            raise CurveError(
                f"{len(self.tiles)} tiles were requested; a screen holds at most {MAX_TILES}")
        # A threshold that cannot be compared against would grade nothing and
        # say nothing about why, which is the silent zero this project exists
        # to remove.
        if not isfinite(self.big) or self.big < 0.0:
            raise CurveError(
                f"the big-move threshold must be a number and not negative; got {self.big!r} "
                f"in decimals ({self.big * 100:g} volatility points). Zero turns the grading "
                f"off")

    def run(self, book, history=None) -> dict:
        results = []
        for tile in self.tiles:
            if not tile.on:
                continue
            try:
                results.append(run_tile(tile, book, history, cut=self.cut,
                                        method=self.method, big=self.big))
            except Exception as exc:  # noqa: BLE001 - one tile keeps its place
                results.append(TileResult(
                    label=tile.default_label(), pair=tile.pair, ok=False,
                    message=str(exc) if isinstance(exc, CurveError)
                    else f"{type(exc).__name__}: {exc}"))
        return {
            "cut": self.cut, "method": self.method, "field": self.field,
            # Volatility points at the edge: `big` is decimals in here and
            # what the screen and the command line print is what was typed.
            "big": self.big * 100.0, "big_step": BIG_MOVE_STEP,
            "moved": sum(r.moved for r in results),
            "moved_hard": sum(r.moved_hard for r in results),
            "fields": [{"key": f, "label": FIELD_LABELS[f]} for f in CURVE_FIELDS],
            "tiles": [r.as_dict() for r in results],
        }


def tile_from_request(item: dict) -> Tile:
    """One tile as the browser posts it.

    Every field the page sends is read here; a test pins that, because a
    setting the server ignores is one that silently does nothing.
    """
    if not isinstance(item, dict):
        raise CurveError("a monitor tile must be an object")
    return Tile(
        pair=str(item.get("pair") or "").strip().upper(),
        now_kind=str(item.get("now_kind") or DEFAULT_NOW_KIND).strip().lower(),
        now_date=str(item.get("now_date") or "").strip(),
        was_kind=str(item.get("was_kind") or DEFAULT_WAS_KIND).strip().lower(),
        was_date=str(item.get("was_date") or "").strip(),
        label=str(item.get("label") or ""),
        on=bool(item.get("on", True)),
    )


def panel_from_request(payload: dict | None) -> MonitorPanel:
    """The whole screen, posted whole."""
    payload = payload or {}
    raw = payload.get("tiles") or []
    if not isinstance(raw, list):
        raise CurveError("'tiles' must be a list of monitor tiles")
    big = payload.get("big")
    if big is None or (isinstance(big, str) and not big.strip()):
        big = DEFAULT_BIG_MOVE
    try:
        big = float(big)
    except (TypeError, ValueError):
        raise CurveError(f"the big-move threshold must be a number of volatility points; "
                         f"got {payload.get('big')!r}") from None
    return MonitorPanel(
        tiles=tuple(tile_from_request(item) for item in raw),
        cut=str(payload.get("cut") or "NY"),
        method=str(payload.get("method") or "SVI"),
        field=str(payload.get("field") or "atm").strip().lower(),
        # The one conversion: volatility points at this edge, decimals inside.
        big=big / 100.0,
    )


def parse_spec(text: str) -> Tile:
    """One ``--watch`` argument: ``PAIR[:was[:now]]``.

    ``was`` and ``now`` are a source, optionally with a date after ``@``:
    ``EURUSD``, ``USDJPY:history@-1m``, ``EURJPY:history@-1w:history@latest``.
    The defaults are the pair's fitted surface now against its historical row
    a week ago, which is the tile somebody adds without thinking about it.
    """
    bits = [b.strip() for b in str(text).split(":")]
    pair = bits[0].upper()
    if not pair:
        raise CurveError(f"{text!r} does not name a currency pair")

    def end(spec: str, default_kind: str, default_date: str) -> tuple[str, str]:
        if not spec:
            return default_kind, default_date
        kind, _, when = spec.partition("@")
        kind = kind.strip().lower() or default_kind
        if kind not in CURVE_KINDS or kind == "paste":
            raise CurveError(
                f"{spec!r}: unknown source {kind!r}; expected one of "
                f"{', '.join(k for k in CURVE_KINDS if k != 'paste')}, optionally with "
                f"'@date', e.g. history@-1w")
        when = when.strip()
        if kind != "history" and when:
            # A date on a source that has none would be read, ignored, and
            # then reported as the live curve under a date somebody chose.
            raise CurveError(f"{kind!r} is the curve as it stands now and takes no date; "
                             f"{spec!r} asks for {when!r}")
        return kind, when

    was_kind, was_date = end(bits[1] if len(bits) > 1 else "",
                             DEFAULT_WAS_KIND, DEFAULT_WAS_DATE)
    now_kind, now_date = end(bits[2] if len(bits) > 2 else "", DEFAULT_NOW_KIND, "")
    return Tile(pair=pair, was_kind=was_kind, was_date=was_date,
                now_kind=now_kind, now_date=now_date)
