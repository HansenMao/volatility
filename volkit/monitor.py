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

Four things are decided here.

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

Everything here is in decimals, like ``curves.py``; the volatility points a
human reads are converted once, by the screen or the command line that prints
them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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

    def as_dict(self) -> dict:
        return {"label": self.label, "pair": self.pair, "ok": self.ok,
                "message": self.message, "now": self.now, "was": self.was,
                "rows": self.rows, "warnings": list(self.warnings),
                "notes": list(self.notes)}


def _end(curve) -> dict:
    return {"kind": curve.kind, "kind_label": KIND_LABELS[curve.kind],
            "source": curve.source, "asof": curve.asof, "ok": curve.ok,
            "message": curve.message}


def run_tile(tile: Tile, book, history=None, *, cut: str = "NY",
             method: str = "SVI") -> TileResult:
    """Build both ends of one tile and difference them."""
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
        row = {
            "tenor": tenor, "t": tenor_to_years(tenor),
            "now": {f: (None if np_ is None else np_.values.get(f)) for f in CURVE_FIELDS},
            "was": {f: (None if wp is None else wp.values.get(f)) for f in CURVE_FIELDS},
            "change": {},
            "message": "; ".join(x for x in ((np_.message if np_ else ""),
                                             (wp.message if wp else "")) if x),
        }
        for f in CURVE_FIELDS:
            a, b = row["now"][f], row["was"][f]
            row["change"][f] = None if a is None or b is None else a - b
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

    def __post_init__(self) -> None:
        if self.field not in CURVE_FIELDS:
            raise CurveError(
                f"unknown field {self.field!r}; expected one of {', '.join(CURVE_FIELDS)}")
        if len(self.tiles) > MAX_TILES:
            raise CurveError(
                f"{len(self.tiles)} tiles were requested; a screen holds at most {MAX_TILES}")

    def run(self, book, history=None) -> dict:
        results = []
        for tile in self.tiles:
            if not tile.on:
                continue
            try:
                results.append(run_tile(tile, book, history, cut=self.cut, method=self.method))
            except Exception as exc:  # noqa: BLE001 - one tile keeps its place
                results.append(TileResult(
                    label=tile.default_label(), pair=tile.pair, ok=False,
                    message=str(exc) if isinstance(exc, CurveError)
                    else f"{type(exc).__name__}: {exc}"))
        return {
            "cut": self.cut, "method": self.method, "field": self.field,
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
    return MonitorPanel(
        tiles=tuple(tile_from_request(item) for item in raw),
        cut=str(payload.get("cut") or "NY"),
        method=str(payload.get("method") or "SVI"),
        field=str(payload.get("field") or "atm").strip().lower(),
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
