"""Scheduled volatility events and their calibration.

An "event" is a dated volatility bump: the user quotes how much a given day's
volatility should rise (in vol points), and the model must back out the height
of a decaying instantaneous-variance spike that reproduces it.

The legacy implementation had two problems.  ``getEventAddOn`` rescanned the
whole event dictionary on *every* quadrature evaluation -- an O(n) dict
comprehension called hundreds of thousands of times per curve.  And
``getEventHeight`` inverted the bump with ``fsolve``, nesting an unchecked
root find around a numerical integration, once per event.

Here the schedule is stored as sorted arrays and evaluated vectorised, and the
inversion is bracketed (the day's volatility is monotone in the event height,
so a bracket always exists and Brent cannot wander).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .numerics import ConvergenceError, solve_scalar
from .timeutil import Clock, as_utc, parse_datetime

# Currencies that share a calendar and a market for the purpose of an event
# weight: a weight given to CNH is the CNY leg's too unless CNY was given its
# own, and the other way round.
CURRENCY_ALIASES: dict[str, str] = {"CNH": "CNY", "CNY": "CNH"}


def pair_legs(pair: str) -> tuple[str, str]:
    """The two currencies of a pair, as written."""
    pair = pair.strip().upper()
    if len(pair) != 6 or not pair.isalpha():
        raise ValueError(f"{pair!r} is not a six-letter currency pair")
    return pair[:3], pair[3:6]


def leg_weights(weights: dict[str, float] | None, pair: str) -> dict[str, float]:
    """Each leg's weight for an event, read off a per-currency table.

    A leg the table does not name weighs nothing; a leg named only through
    its alias (CNH for CNY) takes the alias's weight.  The result always has
    exactly the pair's two legs as keys, in the pair's own spelling, so a
    screen can show two boxes and a file can hold two numbers.
    """
    table = {str(k).upper(): float(v) for k, v in (weights or {}).items()}
    out = {}
    for leg in pair_legs(pair):
        if leg in table:
            out[leg] = table[leg]
        else:
            out[leg] = table.get(CURRENCY_ALIASES.get(leg, ""), 0.0)
    return out


def superpose(a: float, b: float) -> float:
    """Two legs' weights, superimposed into one bump for the pair.

    They **add**.  Two independent surprises add in *variance*, and a quoted
    bump is a variance increment over twice the day's volatility (``(s + b)^2
    - s^2 = 2 s b + b^2``), so to first order two bumps add too; the exact
    variance rule, ``sqrt((s+a)^2 + (s+b)^2 - s^2) - s``, sits between the
    sum and the larger of the two and reaches the sum for bumps small against
    the volatility, which is every real event on every real pair.  A
    root-sum-square would be the rule for two *event volatilities*, and a
    bump is not one.  Where a desk disagrees for a pair -- a release that is
    the same news to both legs and should not count twice -- that is what the
    adjustment is for.
    """
    return float(a) + float(b)


def pair_bump(weights: dict[str, float] | None, pair: str, adjust: float = 0.0) -> float:
    """The bump a pair takes from an event: its two legs superposed, then adjusted."""
    legs = leg_weights(weights, pair)
    a, b = (legs[c] for c in pair_legs(pair))
    return superpose(a, b) + float(adjust)


@dataclass
class EventEntry:
    """One event as asked for, before it is put on a pair's curve.

    ``weights`` is per currency and ``adjust`` is the pair's own, both in
    decimal volatility.  ``bump`` is the total the curve will be calibrated
    to; ``resolve`` works it out for a pair, or, given a bump and no weights,
    reads the whole bump as the adjustment so that ``bump == superposed +
    adjust`` holds for every event on every curve whatever it was typed as.
    """

    when: datetime
    bump: float | None = None
    label: str = ""
    weights: dict[str, float] = field(default_factory=dict)
    adjust: float = 0.0

    def resolve(self, pair: str) -> "EventEntry":
        legs = leg_weights(self.weights, pair)
        if self.weights:
            total = pair_bump(legs, pair, self.adjust)
            if self.bump is not None and abs(self.bump - total) > 1e-12:
                raise ValueError(
                    f"event {self.label or self.when:%Y-%m-%d %H:%M}: bump "
                    f"{self.bump * 100:.4g} does not equal the legs' weights "
                    f"{' + '.join(f'{c} {v * 100:.4g}' for c, v in legs.items())} "
                    f"plus the adjustment {self.adjust * 100:+.4g}; "
                    f"give either the total or its parts"
                )
            return EventEntry(self.when, total, self.label, legs, float(self.adjust))
        bump = float(self.bump if self.bump is not None else self.adjust)
        return EventEntry(self.when, bump, self.label, legs, bump)


def event_entries(rows) -> tuple[list[EventEntry], list[str]]:
    """Rows in the panel's and the session file's spelling, in vol points.

    A row is ``{"when", "label", "bump"}`` or ``{"when", "label", "weights",
    "adjust"}`` -- ``weights`` per currency -- and may carry both when they
    agree, which is what a row a screen showed and posted back does.  Reads
    the timestamp through ``timeutil.parse_datetime`` like every other edge.
    Returns the entries it could read and a message for each it could not,
    so one bad row is named rather than taking the schedule down.
    """
    entries: list[EventEntry] = []
    problems: list[str] = []
    for i, row in enumerate(rows, start=1):
        when = row.get("when")
        if not when:
            problems.append(f"event {i} has no date/time")
            continue
        try:
            dt = parse_datetime(str(when))
        except ValueError as exc:
            problems.append(f"event {i}: {exc}")
            continue
        try:
            raw_w = row.get("weights") or {}
            weights = {str(c).upper(): float(v or 0.0) / 100.0 for c, v in raw_w.items()}
            adjust = float(row.get("adjust") or 0.0) / 100.0
            bump_raw = row.get("bump")
            bump = None if bump_raw in (None, "") else float(bump_raw) / 100.0
        except (TypeError, ValueError, AttributeError):
            problems.append(f"event {i}: weights, adjustment and bump must be numbers")
            continue
        if not weights and bump is None:
            bump = adjust
        if weights and bump is not None and abs(bump - (sum(weights.values()) + adjust)) > 1e-9:
            # The screen shows the total beside its parts; the parts are what
            # was marked, and a total that disagrees with them was computed
            # off something else.
            weights_note = ", ".join(f"{c} {v * 100:g}" for c, v in weights.items())
            problems.append(
                f"event {i}: bump {bump * 100:g} is not the weights ({weights_note}) plus "
                f"the adjustment {adjust * 100:+g}; give the parts or the total, not both"
            )
            continue
        entries.append(EventEntry(dt, bump, str(row.get("label") or ""), weights, adjust))
    return entries, problems


def coerce_entry(item) -> EventEntry:
    """An ``EventEntry``, a ``(when, bump, label)`` tuple or a ``(when, bump)`` pair."""
    if isinstance(item, EventEntry):
        return item
    when, bump, *rest = item
    return EventEntry(when, float(bump), str(rest[0]) if rest else "")


@dataclass
class EventRow:
    """One dated release, as the workbook's EVENTS sheet holds it.

    ``weights`` is what the release is worth on each *currency* and is
    **shared**: every pair with that currency takes it, which is the whole
    reason the sheet has currency columns.  ``adjust`` is each *pair*'s own
    cell on top of its two legs, and is the only thing in the row that
    belongs to one pair.  Both are in decimal volatility, like everything
    else past the reader.
    """

    when: datetime                                            # UTC
    label: str = ""
    weights: dict[str, float] = field(default_factory=dict)   # currency -> decimal
    adjust: dict[str, float] = field(default_factory=dict)    # pair -> decimal

    def __post_init__(self) -> None:
        self.when = as_utc(self.when)
        self.weights = {str(c).upper(): float(v) for c, v in (self.weights or {}).items()}
        self.adjust = {str(p).upper(): float(v) for p, v in (self.adjust or {}).items()}

    def entry(self, pair: str) -> EventEntry:
        """This row as one pair sees it: its legs' weights plus its own cell."""
        pair = pair.upper()
        return EventEntry(self.when, None, self.label, leg_weights(self.weights, pair),
                          self.adjust.get(pair, 0.0)).resolve(pair)

    def touches(self, pair: str) -> bool:
        """Whether the row moves this pair at all.

        A row can sit on the sheet and be nothing to a pair -- a Fed release
        with no cell on EURGBP -- and it still belongs in the pair's panel,
        because that is where its cell would be typed.  What it must not do
        is reach the curve, where a zero bump is an event to calibrate.
        """
        e = self.entry(pair)
        return bool(e.bump) or any(e.weights.values())

    def copy(self) -> "EventRow":
        return EventRow(self.when, self.label, dict(self.weights), dict(self.adjust))


@dataclass
class EventBook:
    """Every event the book knows: the EVENTS sheet in memory.

    This is the one place an event is read from.  A pair's schedule is
    *derived* (``for_pair``) rather than kept beside it, so a currency weight
    cannot come to mean one thing on USDJPY and another on EURJPY -- which is
    exactly what two copies of the same number in two pair columns used to
    allow.
    """

    rows: list[EventRow] = field(default_factory=list)
    source: str = ""

    def sort(self) -> None:
        self.rows.sort(key=lambda r: r.when)

    def copy(self) -> "EventBook":
        """A table nothing else holds a row of.

        The book's live table starts as the workbook's, and the two must not
        be the same object: **Reload** is the workbook's rows again, and a
        session that had been marking straight through the loaded copy would
        have nothing to reload.
        """
        return EventBook([r.copy() for r in self.rows], self.source)

    def currencies(self) -> list[str]:
        """Every currency any row weighs, sorted."""
        return sorted({c for r in self.rows for c in r.weights})

    def row_at(self, when: datetime) -> EventRow | None:
        """The row at this instant, to the minute.  Times are the row's identity."""
        key = as_utc(when).replace(second=0, microsecond=0)
        for r in self.rows:
            if r.when.replace(second=0, microsecond=0) == key:
                return r
        return None

    def for_pair(self, pair: str, *, touching_only: bool = False) -> list[EventEntry]:
        """Every row as this pair sees it, in time order.

        ``touching_only`` is what a curve wants -- an event whose bump is zero
        and whose legs weigh nothing is not an event to calibrate -- while the
        panel wants the whole sheet, blank cells included.
        """
        self.sort()
        return [r.entry(pair) for r in self.rows
                if not touching_only or r.touches(pair)]

    def pairs_weighing(self, currency: str, pairs) -> list[str]:
        """Which of ``pairs`` a weight on this currency reaches.

        A currency is read through the same alias table a leg is
        (``leg_weights``), so a weight on CNY reaches a CNH pair.
        """
        ccy = str(currency).upper()
        out = []
        for p in pairs:
            legs = leg_weights({ccy: 1.0}, p)
            if any(legs.values()):
                out.append(p)
        return out

    # -- editing ---------------------------------------------------------
    def set_pair(self, pair: str, entries, *, pairs=()) -> tuple[list[str], list[str]]:
        """Put one pair's panel back onto the sheet.

        The panel shows the whole sheet through one pair's eyes: its two legs'
        currency columns and its own adjustment cell.  So a weight typed here
        goes into the **shared** row and moves every other pair with that
        currency, and a row deleted here is deleted from the sheet.  Neither
        is hidden: both come back as notes naming the pairs that moved.

        The row's other currencies are untouched -- they are not on this
        panel, and a column nobody showed must not be cleared by a screen
        that never held it.
        """
        pair = pair.upper()
        legs = pair_legs(pair)
        book_pairs = sorted({str(p).upper() for p in pairs} | self._adjust_pairs())
        problems: list[str] = []
        notes: list[str] = []
        keep: list[EventRow] = []
        seen: set[datetime] = set()
        for entry in entries:
            when = as_utc(entry.when).replace(second=0, microsecond=0)
            if when in seen:
                problems.append(
                    f"two events at {when:%Y-%m-%d %H:%M}Z: a row is identified by its "
                    "time, so one of them would overwrite the other")
                continue
            seen.add(when)
            row = self.row_at(when)
            if row is None:
                row = EventRow(when, entry.label)
            else:
                row = row.copy()
                row.when = when
                if entry.label:
                    row.label = entry.label
            for ccy in legs:
                w = float(entry.weights.get(ccy, 0.0))
                moved = [p for p in self.pairs_weighing(ccy, book_pairs) if p != pair]
                if w != row.weights.get(ccy, 0.0) and moved:
                    notes.append(
                        f"{when:%Y-%m-%d %H:%M}Z {ccy} {w * 100:g}: shared, so it moves "
                        + ", ".join(moved))
                if w:
                    row.weights[ccy] = w
                else:
                    row.weights.pop(ccy, None)
            if entry.adjust:
                row.adjust[pair] = float(entry.adjust)
            else:
                row.adjust.pop(pair, None)
            keep.append(row)
        for row in self.rows:
            if row.when.replace(second=0, microsecond=0) in seen:
                continue
            others = sorted(p for p in row.adjust if p != pair)
            if row.weights or others:
                notes.append(
                    f"{row.when:%Y-%m-%d %H:%M}Z removed from the sheet"
                    + (f", which also drops {', '.join(others)}" if others else "")
                    + (f" and the weights {', '.join(sorted(row.weights))}" if row.weights else ""))
        self.rows = keep
        self.sort()
        return problems, notes

    def _adjust_pairs(self) -> set[str]:
        return {p for r in self.rows for p in r.adjust}

    def set_weights(self, rows) -> list[str]:
        """Replace the currency side of the sheet, keeping every pair's cell.

        ``rows`` is the weights panel posted whole: ``{"when", "label",
        "weights"}`` with the weights in decimal volatility.  A row the panel
        does not carry is gone from the sheet, adjustments and all -- the
        panel shows every row, so an absent one was deleted.
        """
        problems: list[str] = []
        fresh: list[EventRow] = []
        seen: set[datetime] = set()
        for i, item in enumerate(rows, start=1):
            try:
                raw = item["when"]
                when = as_utc(raw if isinstance(raw, datetime) else parse_datetime(str(raw)))
                when = when.replace(second=0, microsecond=0)
            except (KeyError, TypeError, ValueError) as exc:
                problems.append(f"weights row {i}: {exc}")
                continue
            if when in seen:
                problems.append(f"weights row {i}: a second row at {when:%Y-%m-%d %H:%M}Z")
                continue
            seen.add(when)
            old = self.row_at(when)
            row = EventRow(when, str(item.get("label") or (old.label if old else "")),
                           {}, dict(old.adjust) if old else {})
            for ccy, w in (item.get("weights") or {}).items():
                try:
                    v = float(w or 0.0)
                except (TypeError, ValueError):
                    problems.append(f"weights row {i}: {ccy} {w!r} is not a number")
                    continue
                if v:
                    row.weights[str(ccy).upper()] = v
            fresh.append(row)
        if not problems:
            self.rows = fresh
            self.sort()
        return problems


# Instantaneous-variance decay rate, per year.  At 5000/yr an event is spent
# within a few hours, which is what confines it to its own volatility day.
DEFAULT_EVENT_DECAY = 5000.0


@dataclass
class Event:
    """A dated volatility bump quoted in decimal volatility."""

    when: datetime
    bump: float
    label: str = ""
    height: float | None = None  # filled in by calibration
    #: Where the bump came from: each leg's weight (per currency, decimal
    #: volatility) and the pair's own adjustment on top.  ``bump`` is always
    #: their total; an event typed as one number carries it as the adjustment.
    weights: dict[str, float] = field(default_factory=dict)
    adjust: float | None = None

    def __post_init__(self) -> None:
        self.when = as_utc(self.when)
        if self.adjust is None:
            self.adjust = self.bump - sum(self.weights.values())


@dataclass
class EventSchedule:
    """A set of events, evaluated as a vectorised instantaneous add-on."""

    events: list[Event] = field(default_factory=list)
    decay: float = DEFAULT_EVENT_DECAY
    window_days: float = 1.0
    # How the quoted bump is interpreted.
    #
    # "forward24h" (default): the bump applies to the 24 hours *following the
    #   event*.  This is what a trader means by "FOMC adds two vols", and it is
    #   the only reading that is stable -- the spike always has its whole life
    #   inside its own window, so the solved height does not depend on where
    #   the event happens to fall relative to an arbitrary 14:00 UTC boundary.
    #
    # "vol_day" (legacy): the bump applies to the NY-cut volatility day that
    #   contains the event.  An event shortly before the 14:00 roll then has
    #   only minutes of its own day left, so the height explodes and the
    #   overflow lands on the following day.  Kept for comparison only.
    window_mode: str = "forward24h"

    def __post_init__(self) -> None:
        self._times = np.zeros(0)
        self._heights = np.zeros(0)

    def add(self, when: datetime, bump: float, label: str = "", *,
            weights: dict[str, float] | None = None, adjust: float | None = None) -> Event:
        ev = Event(when, bump, label, weights=dict(weights or {}), adjust=adjust)
        self.events.append(ev)
        return ev

    def clear(self) -> None:
        self.events.clear()
        self._times = np.zeros(0)
        self._heights = np.zeros(0)

    def sort(self) -> None:
        self.events.sort(key=lambda e: e.when)

    def refresh(self, clock: Clock) -> None:
        """Rebuild the flat arrays used by the vectorised add-on."""
        self.sort()
        calibrated = [e for e in self.events if e.height is not None]
        self._times = np.array([clock.years_to(e.when) for e in calibrated], dtype=float)
        self._heights = np.array([float(e.height) for e in calibrated], dtype=float)

    def addon(self, t: np.ndarray) -> np.ndarray:
        """Instantaneous volatility add-on at times ``t`` (years from valuation).

        Each event contributes ``height * exp(-decay * (t - t_event))`` from its
        own time until the window closes.  Evaluated as a masked broadcast over
        the (few dozen) events rather than a per-point dictionary scan.
        """
        t = np.asarray(t, dtype=float)
        if self._times.size == 0:
            return np.zeros_like(t)
        window = self.window_days / 365.2425
        dt = t[..., None] - self._times  # (..., n_events)
        active = (dt >= 0.0) & (dt <= window)
        contrib = np.where(active, self._heights * np.exp(-self.decay * np.where(active, dt, 0.0)), 0.0)
        return contrib.sum(axis=-1)

    def bumps_in_window(self, end: datetime, days: float = 1.0) -> list[Event]:
        """Events falling in the ``days``-long window ending at ``end``."""
        start = as_utc(end) - timedelta(days=days)
        return [e for e in self.events if start < e.when <= as_utc(end)]

    def total_height_at(self, when: datetime, days: float = 1.0) -> float:
        return sum(float(e.height or 0.0) for e in self.bumps_in_window(when, days))

    def calibrate(self, window_vol_fn, clock: Clock, *, tol: float = 1e-12,
                  max_sweeps: int = 12) -> list[str]:
        """Solve every event height so each reproduces its quoted bump.

        ``window_vol_fn(event, heights)`` returns the volatility of that
        event's window with the given schedule of heights applied.

        Heights are solved **jointly**, by Gauss-Seidel sweeps.  Solving each
        one alone against an event-free curve -- which is what the legacy code
        and the first version here did -- is wrong whenever two events land
        close together: each is calibrated as if it were the only one, and
        once both are switched on neither delivers its quoted bump any more.
        Two events quoted at +2.00 each produced a +4.01 day rather than
        either +2.00 marginal or a stated total.

        Events more than a few hours apart do not interact (the spike is spent
        within about twelve hours), so in practice this converges in one or two
        sweeps and costs no more than the old independent solve.
        """
        problems: list[str] = []
        self.sort()
        live = [e for e in self.events if e.bump != 0.0]
        for ev in self.events:
            if ev.bump == 0.0:
                ev.height = 0.0
            elif ev.height is None:
                ev.height = abs(ev.bump)

        for sweep in range(max_sweeps):
            moved = 0.0
            for ev in live:
                others = {id(e): (e.height or 0.0) for e in live if e is not ev}

                def residual(h: float, _ev=ev, _o=others) -> float:
                    heights = dict(_o)
                    heights[id(_ev)] = h
                    with_ev = window_vol_fn(_ev, heights)
                    heights[id(_ev)] = 0.0
                    without = window_vol_fn(_ev, heights)
                    return (with_ev - without) - _ev.bump

                name = ev.label or ev.when.strftime("%Y-%m-%d %H:%M")
                span = abs(ev.bump) * 400.0 + 1e-6
                try:
                    new = solve_scalar(residual, ev.height or ev.bump,
                                       bracket=(-abs(ev.bump) * 40.0 - 1e-6, span),
                                       xtol=tol, what=f"event height for {name}")
                except (ConvergenceError, ValueError) as exc:
                    new = 0.0
                    problems.append(
                        f"{name}: could not solve for a {ev.bump * 100:+.3f} vol point "
                        f"bump ({exc})"
                    )
                moved = max(moved, abs(new - (ev.height or 0.0)))
                ev.height = new
            if moved <= 1e-10:
                break
        else:
            if live:
                problems.append(
                    f"event heights did not settle in {max_sweeps} sweeps "
                    f"(largest remaining move {moved:.3g}); events may be too close together"
                )
        self.refresh(clock)
        return problems
