"""Vega weights: how far each tenor moves when one of them is moved.

A desk does not mark a term structure one tenor at a time.  It has a view on
the front end, moves it, and the rest of the curve follows in a fixed
proportion -- the 1M goes up a vol, the 3M goes up 0.65 of one, the 1Y 0.40.
Those proportions are the *vega weights*, and until now they lived in
somebody's head and were typed into the overwrite column tenor by tenor.

They are a tab of the workbook now (``Vega Weights``), read here, and two
things use them:

* the **bump** on the marking screen -- a move of one anchor tenor, shared out
  across the curve by the weights and written back as ordinary ATM
  overwrites;
* the **realized weighting** measured off the historical book, which is the
  same shape read out of what the market actually did over a lookback rather
  than out of what a desk believes.

**A weight is a move ratio, not a vega.**  The cell says how far that tenor
moves when the anchor moves 1.00, so the anchor's own weight cancels and only
ratios matter: a column of 2.00 / 1.00 / 0.65 and one of 1.00 / 0.50 / 0.325
are the same curve shape.  The other reading -- a weight proportional to the
tenor's vega, with the move inversely proportional to it -- is a real
convention on other desks and is deliberately *not* this one, because the
number a marker wants to check against the realized table has to be the same
number in both places.

The tab is a ``tenor`` column, a ``default`` column every pair falls back to,
and an optional column per **pair** holding that pair's own shape.  Resolution
is per cell, not per column: a pair column with a blank 2Y takes the default
at 2Y, because a desk that has a view on the front end of USDJPY and none on
its back end should not have to retype the back end to say so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np

#: The workbook tab.  Spelled the way the desk's own workbook already spells
#: it -- this sheet predates the tool that reads it, and renaming somebody's
#: tab to match a naming convention is how a desk's numbers get orphaned.
#: ``configsheets.match_sheet`` finds it however it is now capitalised.  A
#: pair column is added by the marking screen's workbook card or by hand in
#: Excel; nothing here creates one on its own.
VEGA_WEIGHTS_SHEET = "Vega Weights"

#: The column every pair falls back to, cell by cell.
DEFAULT_COLUMN = "default"

#: Columns of the tab that are not weights.  ``note`` is here for the same
#: reason ``PEG_BANDS`` has one: the reasoning a desk wrote down beside a
#: number is worth keeping, and a column the reader refuses is a column
#: nobody can add.
NOT_WEIGHTS = frozenset({"tenor", "note"})


def check_weight(what: str, value: float) -> float:
    """One weight cell, refused here rather than at the bump.

    Finite, and that is all.  A **negative** weight is allowed on purpose: it
    is what a measured beta comes out as when the back end of a curve has been
    trading against the front, and refusing it on the tab would make the
    realized table on the same screen suggest a number the tab cannot hold.
    Zero is allowed too -- a tenor pinned while the rest of the curve moves --
    and is refused only when it is the *anchor*, where it is a division by
    nothing rather than a view.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what}: {value!r} is not a number") from None
    if not math.isfinite(v):
        raise ValueError(f"{what}: {value!r} is not a finite weight")
    return v


@dataclass(frozen=True)
class VegaWeights:
    """The ``Vega Weights`` tab as it stands: tenors down, columns across.

    ``present`` is False for a workbook that has no such tab, which is a
    different answer from a tab somebody emptied -- the first has never been
    given a shape and the second has been given the shape "nothing".
    """

    tenors: tuple[str, ...] = ()
    pairs: tuple[str, ...] = ()
    #: ``{TENOR: {column: weight}}``; the column is ``default`` or a pair.
    cells: dict[str, dict[str, float]] = field(default_factory=dict)
    present: bool = False

    def __bool__(self) -> bool:
        return bool(self.cells)

    def column_for(self, pair: str) -> str:
        """Which column this pair reads: its own if the tab has one."""
        name = str(pair or "").strip().upper()
        return name if name in self.pairs else DEFAULT_COLUMN

    def weight_for(self, pair: str, tenor: str) -> tuple[float | None, str]:
        """``(weight, column)`` for one cell, falling back to ``default``.

        ``(None, "")`` when neither the pair's column nor the default has a
        number at this tenor.  A row that cannot be weighted keeps its place
        on every table that shows one and says why, so this returns the
        absence rather than a one that would look like a marked view.
        """
        row = self.cells.get(str(tenor or "").strip().upper())
        if not row:
            return (None, "")
        own = self.column_for(pair)
        if own != DEFAULT_COLUMN and own in row:
            return (row[own], own)
        if DEFAULT_COLUMN in row:
            return (row[DEFAULT_COLUMN], DEFAULT_COLUMN)
        return (None, "")

    def for_pair(self, pair: str) -> dict[str, float]:
        """``{TENOR: weight}`` for every tenor the tab can weight for a pair."""
        out: dict[str, float] = {}
        for tenor in self.tenors:
            value, _ = self.weight_for(pair, tenor)
            if value is not None:
                out[tenor] = value
        return out


def load_vega_weights(path) -> VegaWeights:
    """Read the workbook's ``Vega Weights`` tab.

    An absent tab is an empty :class:`VegaWeights` with ``present`` False --
    the ordinary case for a workbook that predates it, where the bump simply
    says there is no shape to share a move out by.  A tab that is *there* and
    cannot be read is an error, because a desk that wrote one meant it to
    apply.
    """
    from . import configsheets

    rows = configsheets.read_rows(path, VEGA_WEIGHTS_SHEET,
                                  required=("tenor", DEFAULT_COLUMN))
    if rows is None:
        return VegaWeights()
    tenors: list[str] = []
    pairs: list[str] = []
    cells: dict[str, dict[str, float]] = {}
    for row in rows:
        tenor = row.text("tenor").upper()
        if not tenor:
            continue
        if tenor in cells:
            raise configsheets.ConfigSheetError(
                f"{VEGA_WEIGHTS_SHEET} row {row.number}: {tenor} is on the tab twice, so "
                f"which of the two weights a bump uses would depend on the row order")
        here: dict[str, float] = {}
        for key in row.cells:
            if not key or key in NOT_WEIGHTS:
                continue
            if key != DEFAULT_COLUMN and not _is_pair(key):
                raise configsheets.ConfigSheetError(
                    f"{VEGA_WEIGHTS_SHEET} has a column named {key!r}. Its columns are "
                    f"'tenor', '{DEFAULT_COLUMN}', an optional 'note', and one per pair "
                    f"(six letters, like USDJPY)")
            raw = row.raw(key)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            column = key.upper() if key != DEFAULT_COLUMN else DEFAULT_COLUMN
            try:
                here[column] = check_weight(
                    f"{VEGA_WEIGHTS_SHEET} row {row.number}: {tenor} {column}", raw)
            except ValueError as exc:
                # One exception type for everything wrong with a
                # configuration tab, so a caller catching the reader's errors
                # does not have to know which of them came from here.
                raise configsheets.ConfigSheetError(str(exc)) from None
            if column != DEFAULT_COLUMN and column not in pairs:
                pairs.append(column)
        tenors.append(tenor)
        cells[tenor] = here
    return VegaWeights(tenors=tuple(tenors), pairs=tuple(pairs),
                       cells=cells, present=True)


def _is_pair(key: str) -> bool:
    return len(key) == 6 and key.isalpha()


# ---------------------------------------------------------------------------
# The bump
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BumpRow:
    """One tenor under a bump: what it was, what the weights make of it.

    ``after`` is ``None`` for a row the bump cannot compute, and ``reason``
    says why.  The row keeps its place either way -- a tenor dropped out of
    the table is a tenor a marker thinks was moved.  Volatilities are
    decimals here; the edges convert.
    """

    tenor: str
    weight: float | None
    source: str
    before: float
    after: float | None = None
    reason: str = ""

    @property
    def move(self) -> float | None:
        return None if self.after is None else self.after - self.before


def bump_levels(weights: VegaWeights, pair: str, anchor: str, move: float,
                levels: dict[str, float]) -> list[BumpRow]:
    """Share a move of one tenor out across the curve.

    ``levels`` is ``{TENOR: volatility}`` in decimals and in the order the
    screen shows them; ``move`` is the anchor's move, also in decimals.  Each
    tenor moves ``move * w(tenor) / w(anchor)``, so the anchor moves exactly
    what was asked whatever its own weight is, and a tab scaled to 1.00 at the
    front and one scaled to 100 give the same answer.

    Refused rather than half-applied: an anchor that is not on the curve, or
    that the tab cannot weight, is an error before any row is computed.  A
    single row that would go non-positive is not -- it keeps its place and
    says so, and the rest of the curve still moves.
    """
    order = {t.upper(): t for t in levels}
    key = str(anchor or "").strip().upper()
    if key not in order:
        raise ValueError(
            f"{key or 'no tenor'} is not one of this curve's tenors "
            f"({', '.join(levels) or 'none'}), so it cannot anchor a bump")
    if not weights.present:
        raise ValueError(
            f"this workbook has no {VEGA_WEIGHTS_SHEET} tab, so there is no shape to share "
            f"a move out by. Add it on the Workbook card: a 'tenor' column, a "
            f"'{DEFAULT_COLUMN}' column, and a column per pair that needs its own")
    w_anchor, _ = weights.weight_for(pair, key)
    if w_anchor is None:
        raise ValueError(
            f"{VEGA_WEIGHTS_SHEET} has no weight for {pair} at {key}, so a move of {key} "
            f"cannot be shared out. Give {key} a weight in the '{DEFAULT_COLUMN}' column "
            f"or in {pair}'s own")
    if w_anchor == 0:
        raise ValueError(
            f"{VEGA_WEIGHTS_SHEET} weights {pair} at {key} as {w_anchor:g}. A tenor that "
            f"does not move cannot be the one a move is measured from -- anchor the bump "
            f"somewhere the curve moves")
    out: list[BumpRow] = []
    for tenor, before in levels.items():
        w, source = weights.weight_for(pair, tenor)
        if w is None:
            out.append(BumpRow(tenor=tenor, weight=None, source="", before=before,
                               reason=f"{VEGA_WEIGHTS_SHEET} has no weight for {pair} "
                                      f"at {tenor.upper()}"))
            continue
        after = before + move * w / w_anchor
        if not math.isfinite(after) or after <= 0:
            out.append(BumpRow(tenor=tenor, weight=w, source=source, before=before,
                               reason=f"the bump would take {tenor} to "
                                      f"{after * 100:.4f} vol points"))
            continue
        out.append(BumpRow(tenor=tenor, weight=w, source=source,
                           before=before, after=after))
    return out


# ---------------------------------------------------------------------------
# The same shape, measured
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RealizedWeight:
    """What one tenor did against the anchor over the lookback.

    ``beta`` is the regression of this tenor's daily change in at-the-money
    volatility on the anchor's, which is the move ratio the tab holds.
    ``sd_ratio`` is how much this tenor moved regardless of whether it moved
    *with* the anchor, and ``corr`` is what separates the two: ``beta ==
    corr * sd_ratio`` exactly.  A tenor with a high standard-deviation ratio
    and a low correlation is one that moves a lot on its own, and a bump that
    used its sd-ratio would be marking a move nothing said was coming.
    """

    tenor: str
    beta: float | None = None
    sd_ratio: float | None = None
    corr: float | None = None
    observations: int = 0
    reason: str = ""


@dataclass(frozen=True)
class RealizedWeights:
    """The measured shape of one pair's term structure over a lookback."""

    pair: str
    anchor: str
    lookback_days: float
    rows: tuple[RealizedWeight, ...] = ()
    first: date | None = None
    last: date | None = None
    warnings: tuple[str, ...] = ()

    def suggested(self) -> dict[str, float]:
        """``{TENOR: beta}`` for every tenor that measured one."""
        return {r.tenor: r.beta for r in self.rows if r.beta is not None}


def realized_weights(hist, anchor: str, lookback_days: float, *,
                     end: date | None = None, min_observations: int = 20,
                     tenors=None) -> RealizedWeights:
    """Measure the term structure's shape off the historical book.

    For each tenor the sheet quotes an at-the-money column for, the daily
    *changes* of that column are regressed on the anchor's over the lookback.
    Changes rather than levels, and absolute rather than log changes, because
    what the tab holds is a move ratio in vol points: a 1.00 point move of the
    anchor against a 0.64 point move of the 3M is a weight of 0.64 whatever
    level either of them is at.

    Second moments are taken about **zero**, not about the mean, for the same
    reason every other realized figure here is: the drift in a volatility
    series over a lookback is far smaller than the noise in estimating it, and
    removing an estimated mean from a few dozen daily changes costs more than
    it buys.  The consequence worth knowing is that the anchor's own beta is
    exactly 1 by construction, so a table that shows anything else for it has
    a bug in it.

    A tenor that cannot be measured keeps its place with a reason: the sheet
    quotes nothing there, or the window holds too few paired observations.
    """
    from .history import HistoryError, nearest_quoted_tenor

    key = str(anchor or "").strip().upper()
    if not hist.atm:
        raise HistoryError(
            f"{hist.pair}: the sheet has no at-the-money volatility columns, so nothing "
            f"can be measured against an anchor tenor")
    warnings: list[str] = []
    if key not in hist.atm:
        near = nearest_quoted_tenor(hist.atm, key)
        if near is None:
            raise HistoryError(
                f"{hist.pair}: the sheet quotes no at-the-money volatility at {key}; it "
                f"quotes {', '.join(sorted(hist.atm))}")
        warnings.append(
            f"the sheet quotes no at-the-money volatility at {key}; the weights were "
            f"measured against its {near} column instead")
        key = near
    i, j = hist.window(lookback_days, end)
    dates = hist.dates[i:j]
    base = np.asarray(hist.atm[key][i:j], dtype=float)
    wanted = [t.upper() for t in tenors] if tenors is not None else \
        sorted(hist.atm, key=_years)
    rows: list[RealizedWeight] = []
    first: date | None = None
    last: date | None = None
    for tenor in wanted:
        series = hist.atm.get(tenor)
        if series is None:
            rows.append(RealizedWeight(
                tenor=tenor,
                reason=f"the sheet quotes no at-the-money volatility at {tenor}"))
            continue
        v = np.asarray(series[i:j], dtype=float)
        ok = np.isfinite(base) & (base > 0) & np.isfinite(v) & (v > 0)
        n = int(ok.sum())
        if n - 1 < min_observations:
            rows.append(RealizedWeight(
                tenor=tenor, observations=max(n - 1, 0),
                reason=f"{max(n - 1, 0)} paired change(s) in the last {lookback_days:g} "
                       f"days; at least {min_observations} are needed"))
            continue
        da = np.diff(base[ok])
        dv = np.diff(v[ok])
        saa = float(np.sum(da * da))
        svv = float(np.sum(dv * dv))
        sav = float(np.sum(da * dv))
        if saa <= 0:
            raise HistoryError(
                f"{hist.pair}: the {key} at-the-money volatility did not move over the "
                f"last {lookback_days:g} days, so nothing can be measured against it")
        here = [d for d, keep in zip(dates, ok) if keep]
        first = here[0] if first is None or here[0] < first else first
        last = here[-1] if last is None or here[-1] > last else last
        if svv <= 0:
            rows.append(RealizedWeight(
                tenor=tenor, beta=0.0, sd_ratio=0.0, corr=0.0, observations=int(da.size),
                reason=f"the {tenor} at-the-money volatility did not move over the window"))
            continue
        rows.append(RealizedWeight(
            tenor=tenor, beta=sav / saa, sd_ratio=math.sqrt(svv / saa),
            corr=sav / math.sqrt(saa * svv), observations=int(da.size)))
    thin = [r.tenor for r in rows if r.corr is not None and abs(r.corr) < 0.5]
    if thin:
        warnings.append(
            f"{', '.join(thin)} moved with {key} less than half the time; a beta measured "
            f"through a weak correlation is a small number because the two are unrelated, "
            f"not because that tenor is quiet")
    return RealizedWeights(pair=hist.pair, anchor=key, lookback_days=float(lookback_days),
                           rows=tuple(rows), first=first, last=last,
                           warnings=tuple(warnings))


def _years(tenor: str) -> float:
    from .timeutil import tenor_to_years
    try:
        return tenor_to_years(tenor)
    except ValueError:
        return float("inf")
