"""Discount curves, from the market feed's OIS rows and the FX forwards.

The model still carries no rate curve of its own: what it needs is a discount
factor to one date, twice.

* **Spot delta.**  The market quotes spot delta out to a year on the majors,
  and spot delta is forward delta times the *base* currency's discount factor
  to the option's settlement.  Reading a 25-delta quote as a forward delta
  puts the strike at the wrong place -- by nothing at a week, by most of a
  delta at a year in a 4% currency.
* **The premium as paid.**  A forward premium discounted at the *term*
  currency's rate to the premium date is what actually changes hands.

Both used to come off a ``RATES`` tab of the workbook.  They come off the
market feed now, for one reason: the two factors a pair is priced with are
not independent.  ``F = S x DF_base / DF_term`` is an identity, not a model,
and two deposit curves typed into a spreadsheet do not satisfy it -- the gap
between them is the cross-currency basis, which on a five-year yen trade is
worth more than the smile.  Discounting off curves that disagree with the
forward the same screen shows is how an option and its hedge come out of one
tool at two different prices.

So there is **one stated curve and the rest are implied**:

* ``USDOIS`` rows in the feed are the anchor.  ``USD`` discounts off them and
  nothing else does.
* Every other currency's factor is implied from the anchor through the FX
  forward that currency actually trades on: ``DF_JPY = DF_USD x S/F`` on
  ``USDJPY``, ``DF_EUR = DF_USD x F/S`` on ``EURUSD``.  That is the discount
  rate *including* basis, because the basis is in the forward.
* A cross needs no special case.  ``EURJPY`` discounts off ``DF_EUR`` and
  ``DF_JPY``, each implied through its own dollar leg, and their ratio is the
  composed cross forward exactly -- the feed builds a cross outright from the
  same two legs (``feed.compose_level``), so the identity closes.

A currency the feed states an OIS curve for **still discounts off the implied
factor**: its own curve is read to report the basis (:meth:`DiscountCurves.
basis`) and never to discount, because using it would break the identity
above for every pair it appears in.  The one exception is a feed with no
``USDOIS`` at all: there is then no anchor to be inconsistent with, and a
currency that states its own curve uses it rather than answering nothing.

**Collateral.** Discounting a JPY cashflow at the FX-implied JPY factor is
exactly what a **USD-collateralised CSA** does -- convert at the forward,
discount at USD OIS, convert back at spot, and ``DF_USD x S/F`` is what comes
out.  So the default above is not a convention with no name: it is the USD
CSA, which is what most interbank option business runs under.  A CSA in
another currency discounts the same cashflow differently, and the gap between
the two is the basis:

    PV_Y = A x DF_C(T) x F(Y->C at T) / S(Y->C)

is the whole of it (:meth:`DiscountCurves.csa_df`), with ``C`` the collateral
currency.  ``C = USD`` is the formula above.  ``C = Y`` makes the FX leg
disappear and leaves ``DF_Y`` -- that currency's **own** OIS curve, used
directly, which is the one place a stated non-anchor curve discounts anything.
A collateral currency the feed states no curve for falls back to the USD
reading and says so rather than inventing one.

This is the **premium only**.  A delta is not a discounted cashflow but a
hedge ratio, and the spot delta the market quotes is defined off
``F = S x DF_base/DF_term`` -- so ``df`` above, never ``csa_df``, is what
reaches ``df_foreign``.  Putting a currency's own OIS there would put the
25-delta wings at strikes nobody means.

Rates are per cent per annum, **annually compounded** -- ``DF = (1+r)^-t``,
the convention an OIS is quoted in -- interpolated linearly in years between
the tenors a currency lists and held flat outside them.  A factor that cannot
be got is ``None`` and never a guess: the delta stays a forward delta and the
premium stays undiscounted, and both say so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cross import usd_leg

#: The currency every other one is discounted through.
ANCHOR = "USD"

#: What marks a feed row as a rate rather than a level: ``USDOIS,1W,2.2``.
OIS_SUFFIX = "OIS"

#: A factor outside this is not a discount factor -- a forward the wrong way
#: up, a spot of zero, a rate typed in decimals.  Refused with a reason
#: rather than passed on to a delta that would then be nonsense.
DF_LIMITS = (0.05, 1.5)


def ois_currency(token: str) -> str | None:
    """``"USDOIS"`` -> ``"USD"``; ``None`` for anything that is not an OIS key.

    The feed's first column is a pair everywhere else, and a six-letter token
    ending ``OIS`` cannot be one: there is no ``OIS`` currency.
    """
    key = str(token).strip().upper()
    if len(key) != 6 or not key.endswith(OIS_SUFFIX) or not key[:3].isalpha():
        return None
    return key[:3]


def ois_key(ccy: str) -> str:
    """The feed row a currency's curve is written on."""
    return f"{str(ccy).strip().upper()}{OIS_SUFFIX}"


@dataclass
class OISCurve:
    """One currency's OIS rates by tenor, in decimals, sorted by years."""

    ccy: str
    points: list[tuple[float, float]] = field(default_factory=list)

    def add(self, t: float, rate: float) -> None:
        self.points[:] = sorted([p for p in self.points if p[0] != float(t)]
                                + [(float(t), float(rate))])

    @property
    def tenors(self) -> int:
        return len(self.points)

    def rate(self, t: float) -> float | None:
        """The rate at ``t`` years: linear between the pillars, flat outside."""
        curve = self.points
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

    def df(self, t: float) -> float | None:
        """``(1 + r) ** -t``, or ``None`` with no rows."""
        r = self.rate(t)
        if r is None:
            return None
        return discount_factor(r, t)

    def describe(self) -> str:
        n = self.tenors
        return f"{self.ccy} ({n} tenor{'' if n == 1 else 's'})"


def discount_factor(rate: float, t: float) -> float:
    """``(1 + r) ** -t`` -- annual compounding, the OIS quoting convention."""
    t = max(float(t), 0.0)
    base = 1.0 + float(rate)
    if base <= 0.0:
        raise ValueError(f"a rate of {rate:.4%} has no annually compounded factor")
    return base ** -t


def implied_rate(df: float, t: float) -> float | None:
    """The annually compounded rate a factor stands for.  ``None`` at ``t=0``."""
    if t <= 0 or df <= 0:
        return None
    return df ** (-1.0 / float(t)) - 1.0


@dataclass
class DiscountCurves:
    """Every currency's discount factor: the anchor's stated, the rest implied.

    ``ois`` is what the feed stated, by currency.  ``level`` is how a forward
    is read -- ``(pair, t) -> level dict | None``, which is ``Book._feed_level``
    -- and is a callable rather than a captured feed so that a feed loaded
    *after* the book was built is picked up on the next question, exactly as
    the band's forward lookup is.
    """

    ois: dict[str, OISCurve] = field(default_factory=dict)
    level: object | None = None
    anchor: str = ANCHOR

    # -- what is there ----------------------------------------------------

    @property
    def currencies(self) -> tuple[str, ...]:
        return tuple(sorted(self.ois))

    @property
    def anchored(self) -> bool:
        """True when the feed states the anchor's curve, so the rest can be implied."""
        return bool(self.ois.get(self.anchor, OISCurve(self.anchor)).points)

    def has(self, ccy: str) -> bool:
        """True when ``ccy`` can be discounted at all -- stated or implied."""
        return self.factor(ccy, 0.25)[0] is not None

    def describe(self) -> str:
        if not self.ois:
            return "no OIS curves"
        return ", ".join(self.ois[c].describe() for c in self.currencies)

    # -- the factor -------------------------------------------------------

    def df(self, ccy: str, t: float) -> float | None:
        """The discount factor for ``ccy`` at ``t`` years, or ``None``."""
        return self.factor(ccy, t)[0]

    def factor(self, ccy: str, t: float) -> tuple[float | None, str]:
        """The factor **and how it was got**, which is what the screens say.

        The order is the module docstring's: the anchor off its own curve,
        everything else implied through its dollar leg, and a stated curve
        used only where there is no anchor to be inconsistent with.
        """
        ccy = str(ccy).strip().upper()
        own = self.ois.get(ccy)
        if ccy == self.anchor:
            if own is None or not own.points:
                return None, f"the feed has no {ois_key(ccy)} rows"
            return own.df(t), f"{ois_key(ccy)}"
        if self.anchored:
            df, why = self._implied(ccy, t)
            if df is not None:
                return df, why
            if own is not None and own.points:
                # An anchor that cannot reach this currency -- no dollar leg in
                # the file -- is not a reason to refuse a curve the desk did
                # state.  It is a reason to say which one answered.
                return own.df(t), f"{ois_key(ccy)} ({why}, so it is not implied)"
            return None, why
        if own is not None and own.points:
            return own.df(t), f"{ois_key(ccy)} (the feed has no {ois_key(self.anchor)} rows)"
        return None, f"the feed has no {ois_key(self.anchor)} rows to imply {ccy} from"

    def _implied(self, ccy: str, t: float) -> tuple[float | None, str]:
        """``ccy``'s factor from the anchor's and the FX forward it trades on."""
        anchor_df = self.ois[self.anchor].df(t)
        if anchor_df is None:  # pragma: no cover -- ``anchored`` says otherwise
            return None, f"the feed has no {ois_key(self.anchor)} rows"
        ratio, via = self._fx_ratio(ccy, self.anchor, t)
        if ratio is None:
            return None, via
        df = anchor_df * ratio
        lo, hi = DF_LIMITS
        if not lo <= df <= hi:
            return None, (f"{via} implies a {ccy} discount factor of {df:.4f} "
                          f"at {t:.3g}y, which is not one")
        return df, f"implied from {ois_key(self.anchor)} and the {via} forward"

    def _fx_ratio(self, ccy: str, other: str, t: float) -> tuple[float | None, str]:
        """``F/S`` for one unit of ``ccy`` priced in ``other``, and the pair it read.

        This is the one piece of FX arithmetic in the module and every factor
        goes through it, because ``F = S x DF_base / DF_term`` rearranged the
        wrong way up is a discount factor that looks plausible and is
        reciprocal.  It is read off the pair's **own spelling**: a pair whose
        base is ``ccy`` gives ``F/S`` and one whose base is ``other`` gives
        ``S/F``.  The second returned value is the pair on success and the
        reason on failure, so a caller can say either without asking twice.
        """
        ccy, other = ccy.upper(), other.upper()
        if ccy == other:
            return 1.0, ""
        if self.level is None:
            return None, "there is no feed to imply a forward from"
        # The market's own spelling first, so a file holding both ways up is
        # read the way it is quoted; the reverse after it, for a file that
        # does not.
        try:
            quoted = (usd_leg(ccy) if other == self.anchor else
                      usd_leg(other) if ccy == self.anchor else ccy + other)
        except ValueError as exc:
            return None, str(exc)
        routes: list[str] = []
        for name in (quoted, ccy + other, other + ccy):
            if len(name) == 6 and name.isalpha() and name not in routes:
                routes.append(name)
        if not routes:
            return None, f"{ccy}/{other} is not a currency pair"
        missing: list[str] = []
        for pair in routes:
            level = self._level(pair, t)
            if level is None:
                missing.append(pair)
                continue
            spot, forward = level.get("spot"), level.get("forward")
            if not spot or not forward or spot <= 0 or forward <= 0:
                missing.append(pair)
                continue
            ratio = forward / spot if pair[:3] == ccy else spot / forward
            return ratio, pair + (f" ({level['via']})" if level.get("via") else "")
        return None, f"the feed does not quote {' or '.join(missing)}"

    def _level(self, pair: str, t: float):
        try:
            return self.level(pair, t)
        except Exception:  # noqa: BLE001 -- a feed that cannot answer is no level
            return None

    # -- collateral -------------------------------------------------------

    def csa_df(self, ccy: str, t: float, collateral: str = "") -> tuple[float | None, str]:
        """The factor a cashflow in ``ccy`` is discounted at under a CSA.

        ``collateral`` blank is the anchor, and the anchor's CSA is exactly
        :meth:`factor` -- there is no second arithmetic for it, because there
        is no second answer.  ``collateral == ccy`` is that currency's own OIS
        curve, used directly and only here.  Anything else is the general
        formula in the module docstring.

        The reason to hold this apart from ``df`` rather than making it the
        one lookup: a CSA changes what a *cashflow* is worth and changes no
        hedge ratio.  Only ``pricing._discounted`` may call it.
        """
        ccy = str(ccy).strip().upper()
        want = str(collateral or "").strip().upper() or self.anchor
        if want == self.anchor:
            return self.factor(ccy, t)
        curve = self.ois.get(want)
        if curve is None or not curve.points:
            df, why = self.factor(ccy, t)
            return df, (f"the feed has no {ois_key(want)} rows, so a {want} CSA "
                        f"falls back: {why}")
        collateral_df = curve.df(t)
        if want == ccy:
            return collateral_df, f"{ois_key(want)}, on a {want} CSA"
        ratio, via = self._fx_ratio(ccy, want, t)
        if ratio is None:
            df, why = self.factor(ccy, t)
            return df, f"{via}, so a {want} CSA falls back: {why}"
        df = collateral_df * ratio
        lo, hi = DF_LIMITS
        if not lo <= df <= hi:
            df0, why = self.factor(ccy, t)
            return df0, (f"a {want} CSA through {via} implies a {ccy} factor of "
                         f"{df:.4f} at {t:.3g}y, which is not one; {why}")
        return df, f"implied from {ois_key(want)} and the {via} forward, on a {want} CSA"

    def collateral_currencies(self) -> tuple[str, ...]:
        """The currencies a CSA can be discounted in: the ones with stated rows."""
        return self.currencies

    # -- what the desk's own curve is worth -------------------------------

    def basis(self, ccy: str, t: float) -> float | None:
        """Stated rate less implied rate, in decimals: the cross-currency basis.

        ``None`` unless the feed states this currency's curve *and* the anchor
        can reach it -- there is no basis without two readings of one rate.
        This is the only thing a non-anchor OIS curve is read for, and it is
        worth reading: a basis that moves is a forward the desk should look at.
        """
        ccy = str(ccy).strip().upper()
        own = self.ois.get(ccy)
        if ccy == self.anchor or own is None or not own.points or not self.anchored:
            return None
        stated = own.rate(t)
        df, _ = self._implied(ccy, t)
        if stated is None or df is None:
            return None
        got = implied_rate(df, t)
        if got is None:
            return None
        return stated - got

    def basis_report(self, t: float = 1.0) -> list[dict]:
        """Every stated non-anchor curve against the forwards, at one tenor."""
        out = []
        for ccy in self.currencies:
            b = self.basis(ccy, t)
            if b is None:
                continue
            out.append({"currency": ccy, "years": float(t),
                        "stated": self.ois[ccy].rate(t), "basis": b})
        return out
