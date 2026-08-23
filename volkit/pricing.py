"""Multi-option pricing off a marked book.

The marking side of the tool answers "what is the surface?".  This module
answers "what are these specific options worth on it?" -- a strip of option
legs, each priced against the same marks, with per-leg errors isolated so one
bad row cannot take down the whole ticket.

The model carries no discount curve (the legacy one did not either), so
premiums are undiscounted forward values.  Every output says so.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import black, exotics
from .numerics import ConvergenceError
from .timeutil import UTC, parse_datetime, parse_tenor

# "25d", "25dc", "25 delta put", "-10d", "atm", "dns", or a plain number.
_DELTA_RE = re.compile(r"^\s*(-?)\s*(\d+(?:\.\d+)?)\s*d(?:elta)?\s*([cp])?\s*$", re.IGNORECASE)
# "50d" is the delta-neutral straddle by another name; "0d" is not a strike.
_ATM_WORDS = {"atm", "atmf", "dns", "50d"}


@dataclass
class StrikeSpec:
    """How a leg's strike was specified."""

    kind: str                    # "absolute" | "delta" | "atm"
    value: float = 0.0           # absolute strike, or the delta in (0, 0.5)
    is_call: bool | None = None  # for a delta strike, which side it was quoted on
    side_explicit: bool = False  # True when the text itself said call or put
    text: str = ""


def parse_strike(text) -> StrikeSpec:
    """Accept a number, ``ATM``, or a delta such as ``25d`` / ``10dp`` / ``-25d``.

    Traders quote strikes both ways, so the panel should take both rather than
    forcing a conversion by hand.
    """
    if text is None or (isinstance(text, str) and not text.strip()):
        return StrikeSpec("atm", text="ATM")
    if isinstance(text, (int, float)):
        return StrikeSpec("absolute", float(text), text=str(text))
    s = str(text).strip()
    if s.lower() in _ATM_WORDS:
        return StrikeSpec("atm", text="ATM")
    m = _DELTA_RE.match(s)
    if m:
        sign, number, side = m.group(1), float(m.group(2)), (m.group(3) or "").lower()
        delta = number / 100.0 if number >= 1.0 else number
        if not 0.0 < delta < 0.5:
            raise ValueError(
                f"delta strike {s!r} must be between 0 and 50 delta, got {delta * 100:.4g}"
            )
        if side == "c":
            is_call, explicit = True, True
        elif side == "p":
            is_call, explicit = False, True
        elif sign == "-":
            is_call, explicit = False, True
        else:
            # A bare "25d" does not say which wing; the option type decides.
            is_call, explicit = True, False
        return StrikeSpec("delta", delta, is_call, explicit, text=s)
    try:
        return StrikeSpec("absolute", float(s), text=s)
    except ValueError:
        raise ValueError(
            f"cannot read strike {s!r}; use a number, 'ATM', or a delta like '25d', '10dp', '-25d'"
        ) from None


def resolve_expiry(book, pair: str, text) -> date:
    """Accept a date, or a tenor such as ``1M`` resolved on the pair's calendar."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    s = str(text).strip()
    if not s:
        raise ValueError("expiry is required")
    try:
        parse_tenor(s)
    except ValueError:
        return parse_datetime(s).date()
    # A tenor: go through spot and the delivery date, as the market does.
    return book.calendars.expiry_date(pair, s, book.clock.now.date())


PRODUCTS = ("vanilla", "digital", "one_touch", "no_touch")


@dataclass
class OptionLeg:
    """One column of the pricing panel."""

    pair: str
    expiry: str
    strike: str = "ATM"
    option_type: str = "Auto"     # "C", "P" or "Auto"
    cut: str = "TK"
    method: str = "SVI"
    spot: float | None = None
    forward_points: float = 0.0
    pip: float = 10000.0
    notional: float = 1.0         # in millions of base (payout units for exotics)
    direction: float = 1.0        # +1 bought, -1 sold
    label: str = ""
    # -- exotics --
    product: str = "vanilla"
    barrier: str = ""             # absolute level, for touch products
    ramp_pct: float = 0.0         # digital replication width, % of strike
    overhedge: str = "none"       # none | extend | bend_front | bend_back
    buffer_pct: float = 0.0       # barrier shift, % of barrier
    conservative: bool = True     # overhedge priced against the seller


@dataclass
class LegResult:
    """Everything the panel shows for one leg."""

    ok: bool
    label: str = ""
    pair: str = ""
    error: str = ""
    expiry: str = ""
    days: float = 0.0
    t: float = 0.0
    spot: float = 0.0
    forward: float = 0.0
    strike: float = 0.0
    strike_ratio: float = 0.0
    strike_spec: str = ""
    is_call: bool = True
    vol: float = 0.0
    atm_vol: float = 0.0
    premium_dom: float = 0.0        # domestic per 1 unit of base, undiscounted
    premium_pct_base: float = 0.0   # % of base notional
    delta_pct: float = 0.0          # in the pair's quoted convention
    smile_delta_pct: float = 0.0
    vega_dom: float = 0.0           # domestic per 1 unit of base, per vol point
    gamma: float = 0.0
    premium_amount: float = 0.0     # domestic, for the stated notional
    vega_amount: float = 0.0        # domestic per vol point, for the stated notional
    delta_amount: float = 0.0       # base notional to hedge
    warnings: list[str] = field(default_factory=list)
    # -- exotics --
    product: str = "vanilla"
    barrier: float = 0.0
    barrier_used: float = 0.0       # after any overhedge shift
    fair_value: float = 0.0         # before the overhedge buffer
    overhedge_cost: float = 0.0
    pricing_method: str = "closed form"
    mc_error: float = 0.0
    feed_used: bool = False


def price_leg(book, leg: OptionLeg) -> LegResult:
    """Price one leg, converting any failure into a reported error."""
    try:
        return _price_leg(book, leg)
    except (ValueError, KeyError, ConvergenceError, ZeroDivisionError) as exc:
        return LegResult(ok=False, label=leg.label, pair=leg.pair,
                         error=f"{type(exc).__name__}: {exc}")


def _resolve_market(book, leg: OptionLeg, t: float) -> tuple[float, float, float, bool]:
    """Spot, forward and pip for a leg, from the feed when it has no explicit spot."""
    feed = getattr(book, "feed", None)
    used = False
    spot = float(leg.spot) if leg.spot else None
    pip = float(leg.pip) if leg.pip else None
    points = float(leg.forward_points or 0.0)
    if spot is None and feed is not None and leg.pair.upper() in feed:
        q = feed.quote(leg.pair, t)
        spot, points, pip, used = q["spot"], q["points"], q["pip"], True
    if spot is None:
        spot = 1.0
    if pip is None:
        pip = 10000.0
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot!r}")
    forward = spot + points / pip
    if forward <= 0:
        raise ValueError(
            f"forward is not positive: spot {spot:.6g} with {points:.6g} points"
        )
    return spot, forward, pip, used


def _price_leg(book, leg: OptionLeg) -> LegResult:
    product = (leg.product or "vanilla").strip().lower()
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}; expected one of {PRODUCTS}")
    surface = book[leg.pair]
    expiry = resolve_expiry(book, leg.pair, leg.expiry)
    expiry_dt = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC)
    sl = surface.slice_at(expiry_dt, leg.method, leg.cut)
    t = sl.t
    spot, forward, pip, feed_used = _resolve_market(book, leg, t)

    def vol_at(K_abs: float, *, fwd: float = None, shift: float = 0.0) -> float:
        """Smile vol for an absolute strike, optionally on a shifted surface."""
        return float(surface.vol(K_abs / (fwd or forward), expiry_dt, leg.method, leg.cut)) + shift

    common = dict(
        ok=True, label=leg.label or f"{leg.pair} {expiry:%d%b%y}",
        pair=leg.pair, expiry=expiry.strftime("%Y-%m-%d"),
        days=(expiry_dt - book.clock.now).total_seconds() / 86400.0, t=t,
        spot=spot, forward=forward, atm_vol=sl.atm_vol * 100.0,
        product=product, feed_used=feed_used, warnings=list(sl.warnings),
    )
    band_note = surface.band_check(
        float(leg.barrier) if (leg.barrier and str(leg.barrier).strip()) else None, forward
    ) if (surface.band is not None and leg.barrier and str(leg.barrier).strip()) else []
    common["warnings"] = list(common["warnings"]) + band_note
    notional = float(leg.notional) * float(leg.direction)

    # ---- touch products -------------------------------------------------
    if product in ("one_touch", "no_touch"):
        if not str(leg.barrier).strip():
            raise ValueError(f"{product} needs a barrier level")
        barrier = float(leg.barrier)
        if barrier <= 0:
            raise ValueError(f"barrier must be positive, got {barrier!r}")

        def touch_value(spot_x: float, fwd_x: float, shift: float) -> exotics.TouchResult:
            # The barrier is where the vol is read: that is the level whose
            # dynamics the payout actually depends on.
            v = vol_at(barrier, fwd=fwd_x, shift=shift)
            return exotics.one_touch(
                spot_x, barrier, v, t, fwd_x, is_no_touch=(product == "no_touch"),
                mode=leg.overhedge, buffer_pct=float(leg.buffer_pct or 0.0),
                conservative=bool(leg.conservative))

        res = touch_value(spot, forward, 0.0)
        price, fair = res.price, res.unhedged_price
        vol_used = vol_at(barrier)
        bump = 1e-3
        up = touch_value(spot * (1 + bump), forward * (1 + bump), 0.0).price
        dn = touch_value(spot * (1 - bump), forward * (1 - bump), 0.0).price
        delta = (up - dn) / (2 * bump * spot)
        vega = (touch_value(spot, forward, 0.0001).price
                - touch_value(spot, forward, -0.0001).price) / 2.0
        return LegResult(
            **common, strike=barrier, strike_ratio=barrier / forward,
            strike_spec=f"barrier {barrier:g}", is_call=barrier > spot,
            vol=vol_used * 100.0, barrier=barrier, barrier_used=res.barrier_used,
            fair_value=fair, overhedge_cost=res.overhedge_cost,
            pricing_method=res.method, mc_error=res.std_error,
            premium_dom=price, premium_pct_base=price * 100.0,
            delta_pct=delta * spot * 100.0, smile_delta_pct=float("nan"),
            vega_dom=vega, gamma=0.0,
            premium_amount=notional * price, vega_amount=notional * vega,
            delta_amount=notional * delta * spot,
        )

    # ---- strike-based products ------------------------------------------
    spec = parse_strike(leg.strike)
    if spec.kind == "absolute":
        ratio = spec.value / forward
        strike = spec.value
    elif spec.kind == "atm":
        ratio = float(sl.strikes[2])
        strike = ratio * forward
    else:
        kind_hint = (leg.option_type or "Auto").strip().upper()[:1]
        if spec.side_explicit:
            side_is_call = spec.is_call
        elif kind_hint in ("C", "P"):
            side_is_call = kind_hint == "C"
        else:
            side_is_call = True
        ratio, _ = surface.delta_strike(expiry_dt, spec.value, side_is_call, leg.method, leg.cut)
        strike = ratio * forward
        spec = StrikeSpec("delta", spec.value, side_is_call, True, spec.text)

    kind = (leg.option_type or "Auto").strip().upper()[:1]
    if kind == "C":
        is_call = True
    elif kind == "P":
        is_call = False
    elif spec.kind == "delta" and spec.is_call is not None:
        is_call = spec.is_call
    else:
        is_call = strike >= forward
    vol = vol_at(strike)

    if product == "digital":
        def digi(spot_x: float, fwd_x: float, shift: float) -> exotics.DigitalResult:
            return exotics.european_digital(
                spot_x, strike, t, fwd_x, lambda k: vol_at(k, fwd=fwd_x, shift=shift),
                is_call=is_call, ramp_pct=float(leg.ramp_pct or 0.0),
                conservative=bool(leg.conservative))

        res = digi(spot, forward, 0.0)
        price = res.price
        bump = 1e-3
        up = digi(spot * (1 + bump), forward * (1 + bump), 0.0).price
        dn = digi(spot * (1 - bump), forward * (1 - bump), 0.0).price
        delta = (up - dn) / (2 * bump * spot)
        vega = (digi(spot, forward, 0.0001).price - digi(spot, forward, -0.0001).price) / 2.0
        return LegResult(
            **common, strike=strike, strike_ratio=ratio, strike_spec=spec.text,
            is_call=is_call, vol=vol * 100.0, fair_value=res.fair_value,
            overhedge_cost=res.overhedge_cost,
            pricing_method=(f"call spread {res.strikes[0]:.5g}/{res.strikes[1]:.5g}"
                            if res.ramp else "smile derivative (ramp 0)"),
            premium_dom=price, premium_pct_base=price * 100.0,
            delta_pct=delta * spot * 100.0, smile_delta_pct=float("nan"),
            vega_dom=vega, gamma=0.0,
            premium_amount=notional * price, vega_amount=notional * vega,
            delta_amount=notional * delta * spot,
        )

    # ---- vanilla --------------------------------------------------------
    premium_dom = float(black.price(forward, strike, vol, t, is_call))
    delta = float(black.delta(forward, strike, vol, t, is_call, surface.conv))
    vega = float(black.vega(forward, strike, vol, t)) / 100.0
    gamma = float(black.gamma(forward, strike, vol, t))
    try:
        smile_delta = surface.smile_delta(spot, strike, expiry_dt, is_call, leg.method, leg.cut)
    except (ValueError, ConvergenceError):
        smile_delta = float("nan")
    return LegResult(
        **common, strike=strike, strike_ratio=ratio, strike_spec=spec.text,
        is_call=is_call, vol=vol * 100.0, fair_value=premium_dom,
        premium_dom=premium_dom, premium_pct_base=premium_dom / spot * 100.0,
        delta_pct=delta * 100.0, smile_delta_pct=smile_delta * 100.0,
        vega_dom=vega, gamma=gamma,
        premium_amount=notional * premium_dom, vega_amount=notional * vega,
        delta_amount=notional * delta,
    )


def price_strip(book, legs: list[OptionLeg]) -> dict:
    """Price a strip and total the risk across legs that share a currency pair."""
    results = [price_leg(book, leg) for leg in legs]
    totals: dict[str, dict[str, float]] = {}
    for r in results:
        if not r.ok:
            continue
        bucket = totals.setdefault(r.pair, {"premium": 0.0, "vega": 0.0, "delta": 0.0})
        bucket["premium"] += r.premium_amount
        bucket["vega"] += r.vega_amount
        bucket["delta"] += r.delta_amount
    return {
        "legs": [r.__dict__ for r in results],
        "totals": totals,
        "errors": sum(1 for r in results if not r.ok),
        "note": "premiums are undiscounted forward values; this model carries no rate curve",
    }
