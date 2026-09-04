"""Deposit rates and the discount factors the option conventions need.

The model carries no discount curve and never has: premiums are undiscounted
forward values and every delta was a forward delta.  Two things the market
quotes need a rate all the same, and both need only a discount factor to one
date rather than a curve:

* **Spot delta.**  The market quotes spot delta out to a year on the majors,
  and spot delta is forward delta times the *foreign* (base) currency's
  discount factor to the option's settlement.  Reading a 25-delta quote as a
  forward delta puts the strike at the wrong place -- by nothing at a week,
  by most of a delta at a year in a 4% currency.
* **The premium as paid.**  A forward premium discounted at the *domestic*
  (quote) currency's rate to the premium date is what actually changes hands.

The ``RATES`` tab of the workbook is where a desk states them: ``currency,
tenor, rate`` in per cent per annum, a simple money-market rate, so the
discount factor is ``1 / (1 + r t)``.  Rates are interpolated linearly in
time between the tenors a currency lists and held flat outside them.  A
currency with no rows has no rate: a delta on it stays a forward delta and a
premium stays undiscounted, and both say so.  That is the fallback, never
a guessed rate -- a rate the tool made up would move every wing on a pair
by an amount nobody can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .timeutil import tenor_to_years

#: The workbook tab deposit rates are maintained on.
RATES_SHEET = "RATES"


@dataclass
class RatesTable:
    """Per-currency simple rates by tenor, in decimals, sorted by years."""

    curves: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> "RatesTable | None":
        """Read the ``RATES`` tab.  ``None`` when the workbook has no such tab.

        A row that cannot be read is an error, because a desk that wrote one
        meant it; a blank currency or tenor is a spacer row and is skipped.
        """
        from . import configsheets

        book = Path(path) if path else configsheets.default_workbook()
        rows = configsheets.read_rows(book, RATES_SHEET, required=("currency", "tenor", "rate"))
        if rows is None:
            return None
        table = cls()
        for row in rows:
            ccy, tenor = row.text("currency").upper(), row.text("tenor")
            if not ccy or not tenor:
                continue
            if len(ccy) != 3 or not ccy.isalpha():
                raise ValueError(f"{RATES_SHEET} row {row.number}: {ccy!r} is not a currency")
            rate = row.real("rate")
            if rate is None:
                raise ValueError(f"{RATES_SHEET} row {row.number}: {ccy} {tenor} has no rate")
            try:
                t = float(tenor_to_years(tenor))
            except Exception:  # noqa: BLE001
                raise ValueError(f"{RATES_SHEET} row {row.number}: cannot read the tenor "
                                 f"{tenor!r}") from None
            if not -0.5 < rate < 100.0:
                raise ValueError(f"{RATES_SHEET} row {row.number}: {ccy} {tenor} rate {rate!r} "
                                 f"is not a percentage per annum")
            table.add(ccy, t, rate / 100.0)
        return table

    def add(self, ccy: str, t: float, rate: float) -> None:
        curve = self.curves.setdefault(ccy.upper(), [])
        curve[:] = sorted([p for p in curve if p[0] != t] + [(float(t), float(rate))])

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.curves))

    def has(self, ccy: str) -> bool:
        return bool(self.curves.get(ccy.upper()))

    def rate(self, ccy: str, t: float) -> float | None:
        """The simple rate for ``ccy`` at ``t`` years, or ``None`` with no rows."""
        curve = self.curves.get(ccy.upper())
        if not curve:
            return None
        if t <= curve[0][0]:
            return curve[0][1]
        if t >= curve[-1][0]:
            return curve[-1][1]
        for (t0, r0), (t1, r1) in zip(curve, curve[1:]):
            if t0 <= t <= t1:
                return r0 + (r1 - r0) * (t - t0) / (t1 - t0)
        return curve[-1][1]  # pragma: no cover

    def df(self, ccy: str, t: float) -> float | None:
        """Discount factor to ``t`` years, ``1 / (1 + r t)``, or ``None`` with no rate."""
        r = self.rate(ccy, t)
        if r is None:
            return None
        return 1.0 / (1.0 + r * max(float(t), 0.0))

    def describe(self) -> str:
        return ", ".join(f"{c} ({len(self.curves[c])} tenor{'s' if len(self.curves[c]) != 1 else ''})"
                         for c in self.currencies) or "no rates"
