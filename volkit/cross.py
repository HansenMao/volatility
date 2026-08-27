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


@dataclass
class CrossAtmCurve(AtmCurve):
    """ATM curve for a cross, from two leg curves and a correlation.

    ``sigma_cross^2 = sigma_1^2 + sigma_2^2 - 2 rho sigma_1 sigma_2`` applies
    when both legs share the common currency in the *same* position (both
    quoted against USD as USDXXX, or both as XXXUSD).  ``leg_signs`` flips a
    leg whose quotation is inverted, which is the case the legacy code left
    to the user to get right by hand.
    """

    leg_a: VolCurve | None = None
    leg_b: VolCurve | None = None
    correlation: CorrelationCurve | None = None
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
        """Re-mark the correlation term structure in place."""
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
