"""Cross-pair ATM curves built from two legs and a correlation term structure.

Replaces the legacy ``CVol_Cor`` / ``Vol_Cor`` pair.  Two things are fixed.

The legacy ``Vol_Cor.set_cvol`` took five arguments where the base class's
took seven, and ``Vols.load_vol`` reused the *pair's* initial / long-term /
mean-reversion cells as the correlation's initial / final / decay -- so the
same spreadsheet column meant different things depending on whether the row
was a cross.  Here a cross has its own explicit ``CorrelationCurve``.

The legacy triangle also had no guard on the square root: a correlation
outside [-1, 1] (easy to produce by mis-mapping those columns) silently
produced a negative variance and a bare ``math domain error``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .atm import AtmCurve, VolCurve
from .events import EventSchedule
from .numerics import safe_sqrt
from .timeutil import Clock
from .timeweight import TimeWeighting


@dataclass
class CorrelationCurve:
    """Exponentially decaying correlation between the two legs."""

    initial: float
    final: float
    decay: float = 1.0

    def __post_init__(self) -> None:
        for name, v in (("initial", self.initial), ("final", self.final)):
            if not -1.0 <= v <= 1.0:
                raise ValueError(f"correlation {name} must lie in [-1, 1], got {v:.6g}")
        if self.decay < 0:
            raise ValueError(f"correlation decay must not be negative, got {self.decay:.6g}")

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return self.final - (self.final - self.initial) * np.exp(-self.decay * t)


#: The workbook tab of typed cross correlations the COS export builds its
#: crosses with.  The book never reads it (``publish.py``).
CROSS_CORR_SHEET = "CROSS_CORR"


@dataclass
class MarkedCorrelation:
    """A correlation term structure typed rung by rung, not fitted.

    The alternative to :class:`CorrelationCurve`'s three coefficients.  The
    desk's own cross workbooks carry a correlation **per tenor** -- USDCNH
    against AUDUSD is -0.700 out to 1M, then -0.675, -0.650, -0.625, -0.600 --
    and an exponential fitted through that ladder reproduces none of its rungs
    exactly.  Where the ``CROSS_CORR`` tab names a pair, the COS export builds
    that cross's ATM off its two dollar legs at these marks.  It is **COS
    policy and nothing else**: the book, and every other screen and channel,
    keep the pair's own ``initial`` / ``long_term`` / ``mean_reversion``.

    Between two rungs the correlation is linear in time.  Before the first and
    after the last it is flat, because a desk that stopped typing at 3Y meant
    the last rung to keep applying, not to be extrapolated off a slope it
    never drew.
    """

    years: tuple[float, ...]
    rhos: tuple[float, ...]
    #: The tenor labels the rungs were typed under, for the screen and errors.
    tenors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.years = tuple(float(y) for y in self.years)
        self.rhos = tuple(float(r) for r in self.rhos)
        self.tenors = tuple(str(t) for t in self.tenors)
        if not self.years:
            raise ValueError("a marked correlation needs at least one tenor")
        if len(self.years) != len(self.rhos):
            raise ValueError(
                f"a marked correlation needs one correlation per tenor, got "
                f"{len(self.years)} tenors and {len(self.rhos)} correlations")
        if any(b <= a for a, b in zip(self.years, self.years[1:])):
            raise ValueError("the tenors of a marked correlation must be distinct and "
                             "in increasing order")
        labels = self.tenors or tuple(f"{y:g}y" for y in self.years)
        for label, rho in zip(labels, self.rhos):
            if not -1.0 <= rho <= 1.0:
                raise ValueError(f"correlation {rho:.6g} at {label} must lie in [-1, 1]")

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return np.interp(t, np.asarray(self.years, dtype=float),
                         np.asarray(self.rhos, dtype=float))

    @property
    def marks(self) -> dict[str, float]:
        """``{tenor: rho}`` as it was typed, for the screen."""
        labels = self.tenors or tuple(f"{y:g}y" for y in self.years)
        return dict(zip(labels, self.rhos))

    # A marked ladder read as the three coefficients a cross carries: the
    # first rung, the last rung, and no decay -- there is no exponential here
    # to report.  Nothing reconstructs the rungs from them; the rungs are
    # ``CROSS_CORR``'s.
    @property
    def initial(self) -> float:
        return self.rhos[0]

    @property
    def final(self) -> float:
        return self.rhos[-1]

    @property
    def decay(self) -> float:
        return 0.0


def marked_correlation(pair: str, marks) -> MarkedCorrelation:
    """A :class:`MarkedCorrelation` from ``{tenor: rho}`` as the tab holds it.

    Tenors are read in the workbook's own spellings (``1d``, ``O/N``, ``18M``)
    and sorted by time; a tenor that names no length is refused by name rather
    than dropped, because a rung nobody can place is a rung the curve would
    silently be missing.
    """
    from .kace import canonical_tenor, pillar_years

    rungs: list[tuple[float, str, float]] = []
    for tenor, rho in marks.items():
        if rho is None:
            continue
        try:
            label = canonical_tenor(tenor)
            years = pillar_years(label)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"{pair}: {CROSS_CORR_SHEET} has a row for {tenor!r}, which is "
                             f"not a tenor this reads ({exc})") from exc
        rungs.append((float(years), label, float(rho)))
    if not rungs:
        raise ValueError(f"{pair}: {CROSS_CORR_SHEET} names the pair and gives it no "
                         f"correlation at any tenor")
    rungs.sort()
    seen = {}
    for years, label, rho in rungs:
        if label in seen:
            raise ValueError(f"{pair}: {CROSS_CORR_SHEET} gives {label} two correlations "
                             f"({seen[label]:g} and {rho:g})")
        seen[label] = rho
    return MarkedCorrelation(years=tuple(r[0] for r in rungs),
                             rhos=tuple(r[2] for r in rungs),
                             tenors=tuple(r[1] for r in rungs))


def load_cross_correlations(path, *, overlay=None) -> dict[str, dict[str, float]] | None:
    """Read the workbook's ``CROSS_CORR`` tab: pair, tenor, correlation.

    ``{PAIR: {TENOR: rho}}``, tenors left exactly as they were typed, or
    ``None`` for an absent tab -- the COS export then publishes every cross
    as the book builds it.
    """
    from . import configsheets

    rows = configsheets.read_rows(path, CROSS_CORR_SHEET, required=("pair", "tenor"),
                                  overlay=overlay)
    if rows is None:
        return None
    out: dict[str, dict[str, float]] = {}
    bad: list[str] = []
    for row in rows:
        pair, tenor = row.text("pair").upper(), row.text("tenor")
        if not pair or not tenor:
            continue
        rho = row.real("correlation")
        if rho is None:
            continue
        if not -1.0 <= rho <= 1.0:
            bad.append(f"{CROSS_CORR_SHEET} row {row.number}: {pair} {tenor} correlation "
                       f"{rho:g} is outside [-1, 1]")
            continue
        out.setdefault(pair, {})[str(tenor)] = float(rho)
    if bad:
        raise ValueError("; ".join(bad))
    return out


#: The workbook tab of a cross's marked dependence beyond its correlation: the
#: vol-vol correlation, the correlation vol and the correlation-spot correlation
#: the smile triangle ties the two legs together with (``moments.Dependence``).  The book reads it; a cross it
#: does not name is the Gaussian copula.
CROSS_DEPENDENCE_SHEET = "CROSS_DEPENDENCE"

#: The tab's three value columns, as its header spells them.
VOL_VOL_COLUMN = "vol vol corr"
CORR_VOL_COLUMN = "corr vol"
CORR_SPOT_COLUMN = "corr spot corr"


def load_cross_dependence(path, *, overlay=None) -> dict[str, dict[str, tuple]]:
    """Read the workbook's ``CROSS_DEPENDENCE`` tab: pair, tenor, vol vol corr, corr vol,
    corr spot corr.

    ``{PAIR: {TENOR: (vol_vol, corr_vol, corr_spot)}}``, tenors as typed and any
    value ``None`` where its cell is blank -- a desk may mark one without the
    others.  A tab written before the third column existed has no such column,
    which reads as blank.
    An absent tab is ``{}``: every cross is the Gaussian copula, which is what
    every cross was before the tab existed.

    Refused by row rather than dropped: a pair that is not a cross (a dollar
    pair has no legs to tie together), a tenor that names no length, a vol-vol
    correlation outside ``[-1, 1]``, a correlation vol outside ``[0, 1)``, a
    correlation-spot correlation outside ``[-1, 1]``, and one tenor given twice.
    A row with no value says nothing and is skipped.
    """
    from . import configsheets
    from .timeutil import TenorError, parse_tenor

    rows = configsheets.read_rows(path, CROSS_DEPENDENCE_SHEET, required=("pair", "tenor"),
                                  overlay=overlay)
    if rows is None:
        return {}
    out: dict[str, dict[str, tuple]] = {}
    bad: list[str] = []
    for row in rows:
        pair, tenor = row.text("pair").upper(), row.text("tenor")
        if not pair or not tenor:
            continue
        where = f"{CROSS_DEPENDENCE_SHEET} row {row.number}: {pair} {tenor}"
        vol_vol, corr_vol = row.real(VOL_VOL_COLUMN), row.real(CORR_VOL_COLUMN)
        corr_spot = row.real(CORR_SPOT_COLUMN)
        if vol_vol is None and corr_vol is None and corr_spot is None:
            continue
        if len(pair) != 6 or not pair.isalpha() or not is_cross(pair):
            bad.append(f"{where} -- {pair} is not a cross, so it has no legs to tie together")
            continue
        try:
            parse_tenor(tenor)
        except (TenorError, ValueError) as exc:
            bad.append(f"{where} -- {tenor!r} is not a tenor this reads ({exc})")
            continue
        if vol_vol is not None and not -1.0 <= vol_vol <= 1.0:
            bad.append(f"{where} vol vol corr {vol_vol:g} is outside [-1, 1]")
            continue
        if corr_vol is not None and not 0.0 <= corr_vol < 1.0:
            bad.append(f"{where} corr vol {corr_vol:g} is outside [0, 1)")
            continue
        if corr_spot is not None and not -1.0 <= corr_spot <= 1.0:
            bad.append(f"{where} corr spot corr {corr_spot:g} is outside [-1, 1]")
            continue
        ladder = out.setdefault(pair, {})
        if any(k.upper() == tenor.upper() for k in ladder):
            bad.append(f"{where} is given twice")
            continue
        ladder[tenor] = (vol_vol, corr_vol, corr_spot)
    if bad:
        raise ValueError("; ".join(bad))
    return out


@dataclass
class CrossAtmCurve(AtmCurve):
    """ATM curve for a cross, from two leg curves and a correlation.

    ``sigma_cross^2 = sigma_1^2 + sigma_2^2 - 2 rho sigma_1 sigma_2`` applies
    when both legs share the common currency in the *same* position (both
    quoted against USD as USDXXX, or both as XXXUSD).  ``leg_signs`` flips a
    leg whose quotation is inverted, which is the case the legacy code left
    to the user to get right by hand.

    ``correlation`` is a callable on time, so a fitted curve or a typed
    :class:`MarkedCorrelation` both work.
    """

    leg_a: VolCurve | None = None
    leg_b: VolCurve | None = None
    #: Anything that returns a correlation for a time: the book's fitted
    #: :class:`CorrelationCurve`, or a :class:`MarkedCorrelation`.
    correlation: CorrelationCurve | MarkedCorrelation | None = None
    leg_signs: tuple[int, int] = (1, 1)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.leg_a is None or self.leg_b is None:
            raise ValueError(f"cross curve {self.pair!r} needs both legs")
        if self.correlation is None:
            raise ValueError(f"cross curve {self.pair!r} needs a correlation curve")

    def backbone_vol(self, t):
        """Triangle of the two leg backbones, plus this cross's own add-on."""
        t = np.asarray(t, dtype=float)
        v1 = np.asarray(self.leg_a.backbone_vol(t), dtype=float)
        v2 = np.asarray(self.leg_b.backbone_vol(t), dtype=float)
        rho = np.clip(np.asarray(self.correlation(t), dtype=float), -1.0, 1.0)
        sign = self.leg_signs[0] * self.leg_signs[1]
        var = v1 * v1 + v2 * v2 - 2.0 * sign * rho * v1 * v2
        if np.any(var < -1e-12):
            worst = float(np.min(var))
            raise ValueError(
                f"cross {self.pair!r} produced a negative variance ({worst:.6g}); "
                f"check the correlation curve and the leg quotation signs"
            )
        p = self.params
        return np.sqrt(np.maximum(var, 0.0)) + p.short_addon * np.exp(-p.short_decay * t)

    def set_correlation(self, initial: float, final: float, decay: float) -> list[str]:
        """Re-mark the correlation term structure in place, as three coefficients."""
        try:
            curve = CorrelationCurve(initial, final, decay)
        except ValueError as exc:
            return [str(exc)]
        self.correlation = curve
        self.invalidate()
        if self.events.events:
            self.calibrate_events()
        return []

    def implied_correlation(self, t: float, cross_vol: float) -> float:
        """Back out the correlation that reproduces an observed cross vol."""
        v1 = float(np.asarray(self.leg_a.backbone_vol(t)))
        v2 = float(np.asarray(self.leg_b.backbone_vol(t)))
        if v1 <= 0 or v2 <= 0:
            raise ValueError(f"leg volatilities must be positive, got {v1:.6g} and {v2:.6g}")
        return (v1 * v1 + v2 * v2 - cross_vol * cross_vol) / (2.0 * v1 * v2)


def infer_leg_signs(pair: str, leg_a: str, leg_b: str) -> tuple[int, int]:
    """Work out how two legs compose into a cross.

    Returns the signs to apply in the triangle, so that ``AUDJPY`` from
    ``AUDUSD`` and ``USDJPY`` (common currency in opposite positions) is
    handled differently from ``EURGBP`` from ``EURUSD`` and ``GBPUSD``
    (common currency in the same position).
    """
    pair, leg_a, leg_b = pair.upper(), leg_a.upper(), leg_b.upper()
    base, term = pair[:3], pair[3:6]
    a_base, a_term = leg_a[:3], leg_a[3:6]
    b_base, b_term = leg_b[:3], leg_b[3:6]
    common = ({a_base, a_term} & {b_base, b_term}) - {base, term}
    if not common:
        raise ValueError(
            f"legs {leg_a} and {leg_b} share no third currency, so they cannot build {pair}"
        )
    # A shared third currency is not on its own enough: EURUSD and EURUSD
    # share the dollar and build nothing, and a sheet that names two legs by
    # hand can say exactly that.  Each side of the pair has to come from a
    # different leg.
    a_ccy, b_ccy = {a_base, a_term}, {b_base, b_term}
    if not ((base in a_ccy and term in b_ccy) or (base in b_ccy and term in a_ccy)):
        raise ValueError(
            f"legs {leg_a} and {leg_b} do not carry {base} and {term} between them, "
            f"so they cannot build {pair}"
        )
    c = common.pop()
    # +1 if the leg reads as (pair currency)/(common currency), -1 if inverted.
    sign_a = 1 if a_term == c else -1
    sign_b = 1 if b_term == c else -1
    return (sign_a, sign_b)


#: Currencies the market quotes with the dollar as the *term* currency, so
#: their dollar leg is written ``XXXUSD``.  Everything else is written
#: ``USDXXX``.  This is quotation and not a preference -- nobody publishes
#: ``USDEUR`` -- and getting it wrong is not cosmetic: a leg written upside
#: down is a leg the feed does not hold, and it enters the triangle with the
#: other sign (see :func:`infer_leg_signs`), which is exactly the mistake
#: MIGRATION.md's first entry is about.
USD_TERM_CURRENCIES = frozenset({"EUR", "GBP", "AUD", "NZD"})


def usd_leg(ccy: str) -> str:
    """The pair in which ``ccy`` trades against the dollar.

    ``JPY`` -> ``USDJPY``, ``EUR`` -> ``EURUSD``.
    """
    ccy = str(ccy).strip().upper()
    if len(ccy) != 3 or not ccy.isalpha():
        raise ValueError(f"{ccy!r} is not a three-letter currency code")
    if ccy == "USD":
        raise ValueError("USD has no dollar leg of its own")
    return f"{ccy}USD" if ccy in USD_TERM_CURRENCIES else f"USD{ccy}"


def is_cross(pair: str) -> bool:
    """True when neither side of the pair is the dollar."""
    pair = str(pair).strip().upper()
    return "USD" not in (pair[:3], pair[3:6])


def dollar_legs(pair: str) -> tuple[str, str]:
    """The two dollar pairs a cross is built from, base leg first.

    ``AUDJPY`` -> ``("AUDUSD", "USDJPY")``, ``EURGBP`` -> ``("EURUSD",
    "GBPUSD")``, ``EURCNH`` -> ``("EURUSD", "USDCNH")``.

    The order is not arbitrary and is relied on twice: the first leg carries
    the cross's base currency and the second its term currency, which is what
    ``Book._feed_level`` composes an outright with and what
    :func:`infer_leg_signs` reads the triangle's signs out of.  A cross whose
    legs are named in the sheet is left exactly as it was named; this is only
    what to do when nobody named them.
    """
    pair = str(pair).strip().upper()
    if len(pair) != 6 or not pair.isalpha():
        raise ValueError(f"{pair!r} is not a six-letter currency pair")
    base, term = pair[:3], pair[3:6]
    if base == term:
        raise ValueError(f"{pair} is one currency against itself")
    if not is_cross(pair):
        raise ValueError(f"{pair} is already a dollar pair, so it has no legs")
    return (usd_leg(base), usd_leg(term))
