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


def expiry_datetime(book, pair: str, text) -> datetime:
    """A typed expiry as an instant: a tenor on the calendar, or a date as given.

    One box takes both, because a desk writes both: ``1W`` and ``8d`` go
    through spot and the delivery date on the pair's own calendar, as the
    market does, and anything else is handed to ``timeutil.parse_datetime``,
    which reads the tabular formats, the spellings a person types
    (``28May24``, ``28 May 2024``, ``2024/05/28``) and ISO 8601.

    A time of day survives.  A tenor has none and lands at midnight UTC; a
    string that carried one keeps it, because an option struck at a cut is
    not the option struck at midnight.
    """
    if isinstance(text, datetime):
        return text if text.tzinfo else text.replace(tzinfo=UTC)
    if isinstance(text, date):
        return datetime.combine(text, datetime.min.time()).replace(tzinfo=UTC)
    s = str(text).strip()
    if not s:
        raise ValueError("expiry is required")
    try:
        parse_tenor(s)
    except ValueError:
        return parse_datetime(s)
    return datetime.combine(book.calendars.expiry_date(pair, s, book.clock.now.date()),
                            datetime.min.time()).replace(tzinfo=UTC)


def resolve_expiry(book, pair: str, text) -> date:
    """Accept a date, or a tenor such as ``1M`` resolved on the pair's calendar."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    return expiry_datetime(book, pair, text).date()


PRODUCTS = ("vanilla", "digital", "one_touch", "no_touch")


def resolve_legs(book, rows) -> list[dict]:
    """What each leg's expiry and market boxes resolve to, without pricing it.

    The pricing screen's three market boxes -- spot, the outright forward and
    the expiry -- are filled from this: a tenor or one of the date spellings
    ``timeutil`` reads comes back as the one standard date, and the feed's
    level at *that* expiry comes back beside it.  It is deliberately not a
    price: it is called while somebody is still typing, and it must not wait
    for a smile.

    The level is ``Book.market_level``, the one place a level is read, so a
    cross the feed quotes only through its legs answers here exactly as it
    does when the leg is priced.  Asking the feed for the pair *by name*, as
    this screen's fill button used to, refused EURJPY off a file quoting both
    of its legs while the pricing beneath it went through perfectly well.

    A row that cannot be resolved keeps its place and carries the reason, and
    a row whose expiry resolved but whose market did not keeps the expiry:
    the two are separate failures and one does not hide the other.
    """
    out: list[dict] = []
    for i, row in enumerate(rows or []):
        r: dict = {"index": i, "pair": str(row.get("pair") or "").upper(),
                   "text": str(row.get("expiry") or ""), "expiry": "",
                   "days": None, "years": None, "spot": None, "forward": None,
                   "points": None, "pip": None, "extrapolated": False,
                   "feed": False, "derived": False, "via": "", "error": ""}
        try:
            if not r["pair"]:
                raise ValueError("the leg has no currency pair")
            expiry = resolve_expiry(book, r["pair"], r["text"])
            when = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC)
            t = book.clock.years_to(when)
            r.update(expiry=expiry.isoformat(), years=t,
                     days=(when - book.clock.now).total_seconds() / 86400.0)
            level = book.market_level(r["pair"], t)
            if not level["feed"]:
                raise ValueError(
                    f"the feed does not quote {r['pair']}"
                    + ("" if book.feed is None else
                       ", and it does not quote both of the legs it could be built from")
                )
            r.update(spot=float(level["spot"]), forward=float(level["forward"]),
                     points=float(level["points"]), pip=float(level["pip"]),
                     extrapolated=bool(level["extrapolated"]), feed=True,
                     derived=bool(level["derived"]), via=level["via"])
        except (ValueError, KeyError, TypeError) as exc:
            r["error"] = str(exc)
        out.append(r)
    return out


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
    forward: float | None = None   # outright; blank takes the feed's
    forward_points: float | None = None   # the other spelling: points on spot
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
    market_source: str = "typed"    # which half of the market the feed gave
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
    feed_used: bool = False         # the feed gave the spot, the forward, or both


def price_leg(book, leg: OptionLeg) -> LegResult:
    """Price one leg, converting any failure into a reported error."""
    try:
        return _price_leg(book, leg)
    except (ValueError, KeyError, ConvergenceError, ZeroDivisionError) as exc:
        return LegResult(ok=False, label=leg.label, pair=leg.pair,
                         error=f"{type(exc).__name__}: {exc}")


def _resolve_market(book, leg: OptionLeg, t: float):
    """Spot and the outright forward for a leg: what the leg says, else the feed.

    The pricing screen shows **one box each** for spot and the forward and
    fills both from the feed, so a leg normally arrives with both and this
    reads them straight off -- what is priced is what is on the screen, and
    ``market_source`` says "typed" because it was.  A box left empty falls
    back to the feed on its own: clearing the forward and keeping a
    hand-typed spot is an ordinary thing to do to one leg of a strip, and it
    should not need the other box cleared as well.

    The forward is the **outright**, in the pair's own units, because that is
    what the screen shows and what the model uses.  ``forward_points`` /
    ``pip`` are the other spelling, for a caller holding points rather than an
    outright; giving them is itself a statement of where the forward is, so
    the feed does not then fill it in.  That is why they default to ``None``
    and not to zero -- a leg that named its own spot to override the feed and
    said nothing about points wants the feed's forward, and a leg that said
    ``forward_points=0`` wants the forward *at* spot.  Nothing else can tell
    those two apart.

    Through ``Book.market_level``, which is the one place a level is read, so
    a cross the feed quotes only through its legs fills a blank box here
    exactly as it scales the marking screen's strike axis.  ``via`` names the
    legs when that happened and is empty when the pair was quoted itself.
    """
    spot = float(leg.spot) if leg.spot else None
    forward = float(leg.forward) if leg.forward else None
    points = None if leg.forward_points is None else float(leg.forward_points)
    pip = float(leg.pip) if leg.pip else None
    used_spot = used_fwd = False
    via = ""
    if spot is None or (forward is None and points is None):
        level = book.market_level(leg.pair, t)
        if level["feed"]:
            via = level["via"]
            if pip is None:
                pip = float(level["pip"])
            if spot is None:
                spot, used_spot = float(level["spot"]), True
            if forward is None and points is None:
                forward, used_fwd = float(level["forward"]), True
    if pip is None:
        pip = 10000.0
    if spot is None:
        # No feed and nothing typed.  An outright on its own is still a
        # market: this model carries no discount curve, so spot has nothing
        # to say here that the forward has not already said.
        spot = forward if forward is not None else 1.0
    if spot <= 0:
        raise ValueError(f"spot must be positive, got {spot!r}")
    if forward is None:
        forward = spot + (points or 0.0) / pip
    if forward <= 0:
        raise ValueError(
            f"forward is not positive: spot {spot:.6g}, forward {forward:.6g}"
        )
    source = ("feed" if used_spot and used_fwd else
              "spot from the feed" if used_spot else
              "forward from the feed" if used_fwd else "typed")
    return spot, forward, pip, source, via


def _price_leg(book, leg: OptionLeg) -> LegResult:
    product = (leg.product or "vanilla").strip().lower()
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}; expected one of {PRODUCTS}")
    surface = book[leg.pair]
    expiry = resolve_expiry(book, leg.pair, leg.expiry)
    expiry_dt = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC)
    sl = surface.slice_at(expiry_dt, leg.method, leg.cut)
    t = sl.t
    spot, forward, pip, market_source, feed_via = _resolve_market(book, leg, t)

    def vol_at(K_abs: float, *, fwd: float = None, shift: float = 0.0) -> float:
        """Smile vol for an absolute strike, optionally on a shifted surface."""
        return float(surface.vol(K_abs / (fwd or forward), expiry_dt, leg.method, leg.cut)) + shift

    common = dict(
        ok=True, label=leg.label or f"{leg.pair} {expiry:%d%b%y}",
        pair=leg.pair, expiry=expiry.strftime("%Y-%m-%d"),
        days=(expiry_dt - book.clock.now).total_seconds() / 86400.0, t=t,
        spot=spot, forward=forward, atm_vol=sl.atm_vol * 100.0,
        product=product, market_source=market_source,
        feed_used=market_source != "typed", warnings=list(sl.warnings),
    )
    if feed_via:
        common["warnings"] = list(common["warnings"]) + [
            f"the feed does not quote {leg.pair.upper()}; spot and the outright forward "
            f"came from the {feed_via} triangle"]
    notional = float(leg.notional) * float(leg.direction)

    def band_note(level: float) -> None:
        """Flag a level a lognormal smile has no business pricing.

        The level checked is the one the payout actually depends on: the
        barrier for a touch, the strike for everything else.  An earlier cut
        only ever looked at ``leg.barrier``, so a vanilla or a digital struck
        outside a managed band went through unflagged -- the same silence, for
        a product far more likely to be struck there.  It also read a barrier
        left on a leg whose product no longer uses one.  The leg's own method
        goes in, so a leg already priced with ``BAND`` is not told to use it.
        """
        if surface.band is None:
            return
        common["warnings"] = list(common["warnings"]) + surface.band_check(
            level, forward, leg.method)

    # ---- touch products -------------------------------------------------
    if product in ("one_touch", "no_touch"):
        if not str(leg.barrier).strip():
            raise ValueError(f"{product} needs a barrier level")
        barrier = float(leg.barrier)
        if barrier <= 0:
            raise ValueError(f"barrier must be positive, got {barrier!r}")
        band_note(barrier)

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

    band_note(strike)

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
