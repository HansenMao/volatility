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
from dataclasses import dataclass, field, replace
from datetime import date, datetime

from . import black, exotics
from .numerics import ConvergenceError
from .timeutil import UTC, parse_datetime, parse_tenor

# "25d", "25dc", "25 delta put", "-10d", "atm", "dns", or a plain number.
_DELTA_RE = re.compile(r"^\s*(-?)\s*(\d+(?:\.\d+)?)\s*d(?:elta)?\s*([cp])?\s*$", re.IGNORECASE)
# Three ways of asking for the money.  ``ATM`` is the pair's own convention
# at that tenor -- the delta-neutral straddle out to the boundary, the
# forward beyond it -- and the other two name one of those outright: a desk
# that types ``ATMF`` on a 3M wants the forward strike, not the straddle the
# convention would give it, and ``DNS`` (or ``50d``) on a 2Y wants the
# straddle.  ``0d`` is not a strike.
_ATM_WORDS = {"atm": "convention", "atmf": "forward", "fwd": "forward", "forward": "forward",
              "dns": "straddle", "50d": "straddle", "straddle": "straddle"}


@dataclass
class StrikeSpec:
    """How a leg's strike was specified."""

    kind: str                    # "absolute" | "delta" | "atm"
    value: float = 0.0           # absolute strike, or the delta in (0, 0.5)
    is_call: bool | None = None  # for a delta strike, which side it was quoted on
    side_explicit: bool = False  # True when the text itself said call or put
    text: str = ""
    atm_kind: str = "convention"  # for an ATM strike: "convention" | "forward" | "straddle"


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
    atm_kind = _ATM_WORDS.get(s.lower())
    if atm_kind:
        label = {"convention": "ATM", "forward": "ATMF", "straddle": "DNS"}[atm_kind]
        return StrikeSpec("atm", text=label, atm_kind=atm_kind)
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


def resolve_strike(surface, text, slice_, forward: float, expiry_dt, *,
                   method: str | None = None, cut: str = "TK",
                   option_type: str = "Auto") -> tuple[float, float, StrikeSpec]:
    """Where a typed strike lands on the marks: ratio, absolute level, and what was asked.

    ``ATM`` and ``25d`` are ways of *asking for* a strike and both are answered
    on the surface -- the delta-neutral straddle's own moneyness for the first,
    a solve on the interpolated smile for the second.  An absolute number is
    taken as written.  The spec that comes back is the request as resolved, so
    a bare ``25d`` reports the wing it was read on.

    There is one of these because a strike read two ways is a strike that can
    be read two different ways: the pricing grid and the marking screen's vol
    query ask the same question of the same marks and must never differ on
    which strike the answer is at.
    """
    spec = parse_strike(text)
    if spec.kind == "absolute":
        return spec.value / forward, spec.value, spec
    if spec.kind == "atm":
        if spec.atm_kind == "forward":
            ratio = 1.0
        elif spec.atm_kind == "straddle":
            ratio = float(black.dns_strike(1.0, slice_.atm_vol, slice_.t, surface.conv))
        else:
            ratio = float(slice_.strikes[2])
        return ratio, ratio * forward, spec
    kind_hint = (option_type or "Auto").strip().upper()[:1]
    if spec.side_explicit:
        side_is_call = spec.is_call
    elif kind_hint in ("C", "P"):
        side_is_call = kind_hint == "C"
    else:
        # A bare "25d" names two strikes; the call is the one `parse_strike`
        # takes, and the row says which wing it was answered on.
        side_is_call = True
    ratio, _ = surface.delta_strike(expiry_dt, spec.value, side_is_call, method, cut)
    return ratio, ratio * forward, StrikeSpec("delta", spec.value, side_is_call, True, spec.text)


def leg_dates(book, pair: str, text, settle=None):
    """The FX date bundle behind one leg's expiry box.

    A tenor is resolved on the pair's calendar -- spot date, settlement date,
    and the expiry back from it -- and a date typed straight in is taken as
    the expiry, with the same settlement lag applied to it.  One function, so
    the settlement date a leg *shows* and the settlement date its forward is
    read on are the same date; and so a screen never has to know which of the
    two spellings somebody used.

    ``settle`` is a settlement date **stated** rather than derived, and is the
    one thing about a leg's dates the calendar cannot be asked for: a broken
    date, a trade the counterparty settles a day late, a corporate date agreed
    away from the standard lag.  It moves the settlement date and nothing
    else -- the expiry is what the option is worth time on, and it stays where
    it was typed -- and because this is the one construction, the date the
    screen shows and the date the forward is read on move together.  A blank
    hands the leg back to the calendar, which is how every other overridable
    box on this screen is handed back.

    A stated date **before the expiry** is refused: an option cannot settle
    before it is exercised, and a forward read there would be a forward to a
    date the trade has not reached.  A stated date the calendar would not
    settle on is allowed and *reported* -- that is exactly the case the box
    exists for, and it is the desk's to make, not this function's to refuse.
    """
    if isinstance(text, datetime):
        dates = book.calendars.dates_for_expiry(pair, text.date(), book.clock.now.date())
    elif isinstance(text, date):
        dates = book.calendars.dates_for_expiry(pair, text, book.clock.now.date())
    else:
        s = str(text).strip()
        if not s:
            raise ValueError("expiry is required")
        try:
            parse_tenor(s)
        except ValueError:
            dates = book.calendars.dates_for_expiry(
                pair, parse_datetime(s, today=book.clock.now.date()).date(),
                book.clock.now.date())
        else:
            dates = book.calendars.fx_dates(pair, s, book.clock.now.date())
    if settle is None or (isinstance(settle, str) and not settle.strip()):
        return dates
    stated = book.stated_date(settle)
    if stated < dates.expiry:
        raise ValueError(
            f"the settlement date {stated.isoformat()} is before the expiry "
            f"{dates.expiry.isoformat()}; an option settles on or after the day it "
            "expires. Empty the box to settle on the pair's own calendar")
    return replace(dates, delivery=stated,
                   rule=f"settlement date as typed; the calendar would settle this expiry "
                        f"on {dates.delivery.isoformat()}")


def settlement_note(book, pair: str, dates, stated: bool) -> str:
    """What is worth saying out loud about a *stated* settlement date.

    Only ever about a date somebody typed: one the calendar produced is
    already a value date by construction, and a note on every leg is a note
    nobody reads.  A stated date that is not a value date for the pair is the
    thing to say -- it is deliverable only by agreement -- and it is a note
    and not a refusal, because a broken date is precisely what the box is
    there to hold.
    """
    if not stated:
        return ""
    if book.calendars.is_settlement_day(pair, dates.delivery):
        return ""
    return (f"{dates.delivery.isoformat()} is not a value date for {pair.upper()}; "
            "the forward is read there anyway")


def expiry_datetime(book, pair: str, text) -> datetime:
    """A typed expiry as an instant: a tenor on the calendar, or a date as given.

    One box takes both, because a desk writes both: ``1W`` and ``8d`` go
    through spot and the delivery date on the pair's own calendar, as the
    market does, and anything else is handed to ``timeutil.parse_datetime``,
    which reads the tabular formats, the spellings a person types
    (``28May24``, ``28 May 2024``, ``2024/05/28``) and ISO 8601.  A date with
    no year on it (``28 May``) is the next one of it, resolved forward from
    the book's clock.

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
        # The book's clock says which year a date written without one means.
        return parse_datetime(s, today=book.clock.now.date())
    return datetime.combine(leg_dates(book, pair, s).expiry,
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

    The level is ``Book.market_level_for``, so it is read **on the leg's own
    settlement date** -- the date the forward is actually a price for -- and a
    cross the feed quotes only through its legs answers here exactly as it
    does when the leg is priced.  Asking the feed for the pair *by name*, as
    this screen's fill button used to, refused EURJPY off a file quoting both
    of its legs while the pricing beneath it went through perfectly well.

    The spot date and the settlement date come back with it, because a screen
    that shows a forward should be able to say what date it is a forward to,
    and because the settlement date is the one the desk confirms a trade on.
    The settlement date is also an **input**: a row may state one, and then it
    is that date the level is read on.  ``settle_default`` is the date the
    calendar would have produced, which is what a box holding an override is
    handed back to when it is emptied -- so the screen never has to rebuild
    the construction to know what the default was.

    A row that cannot be resolved keeps its place and carries the reason, and
    a row whose expiry resolved but whose market did not keeps the expiry:
    the two are separate failures and one does not hide the other.
    """
    out: list[dict] = []
    for i, row in enumerate(rows or []):
        # A settlement date is an override only when it was *stated*.  The
        # screen fills the box with the calendar's own date and flags it
        # `calc`, exactly as it fills spot from the feed, so the date being
        # in the box is not evidence anybody chose it.  A caller that sends a
        # date and no flag -- the API, a script -- means it.
        settle_in = str(row.get("settle") or "").strip()
        stated = (str(row.get("settlesrc") or "typed").strip().lower() == "typed"
                  and bool(settle_in))
        r: dict = {"index": i, "pair": str(row.get("pair") or "").upper(),
                   "text": str(row.get("expiry") or ""), "expiry": "",
                   "spot_date": "", "settle": "", "settle_default": "",
                   "settle_rule": "", "settle_stated": stated, "settle_note": "",
                   "days": None, "years": None, "spot": None, "forward": None,
                   "points": None, "pip": None, "extrapolated": False,
                   "feed": False, "derived": False, "via": "",
                   "error": ""}
        try:
            if not r["pair"]:
                raise ValueError("the leg has no currency pair")
            dates = leg_dates(book, r["pair"], r["text"], settle_in if stated else None)
            expiry = dates.expiry
            when = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC)
            t = book.clock.years_to(when)
            r.update(expiry=expiry.isoformat(), years=t,
                     spot_date=dates.spot.isoformat(), settle=dates.delivery.isoformat(),
                     settle_default=book.settlement_date(r["pair"], expiry).isoformat(),
                     settle_rule=dates.rule,
                     settle_note=settlement_note(book, r["pair"], dates, stated),
                     days=(when - book.clock.now).total_seconds() / 86400.0)
            level = book.market_level_for(r["pair"], expiry, dates.delivery)
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


def quick_vol(book, pair: str, expiry, strike="ATM", *,
              cut: str = "TK", method: str | None = None,
              forward: float | None = None) -> dict:
    """One reading of the marked surface: an expiry, a strike, and the vol there.

    The marking screen's own question, and the smallest one this tool asks.
    It takes the two boxes the pricing screen takes and reads them the same
    way -- ``expiry_datetime`` for a tenor or any of the date spellings,
    ``resolve_strike`` for ``ATM``, an absolute level or a delta -- and it
    prices nothing: there is no notional, no option type, no premium and no
    greek, because the answer to "where is the 3M 25 delta marked" is a
    volatility and everything else on a pricing row is noise around it.

    A bare ``25d`` names two strikes and is read on the call, as it is on the
    pricing screen; the **strike itself** is where the other wing is asked for
    (``25dp``, ``-25d``), which is why there is no option type here to say it
    a second way.  At an absolute strike or the at-the-money there is nothing
    to say either: the volatility there is one number for the call and the
    put, which is `quotes._settle_side`'s rule in the other direction.

    **The forward comes from the feed**, through ``Book.market_level_for``,
    which reads it on the expiry's own **settlement date** (and so a cross the
    feed quotes only through its legs is answered from them, with ``via``
    naming the triangle).
    That is what puts an absolute strike on the same axis as the marks, which
    are in moneyness.  Without a feed the smile is still perfectly readable in
    ``K/F`` -- ``ATM`` and a delta are moneyness questions and need no level
    at all -- so those answer and say the axis they answered on, exactly as
    the smile chart does; an **absolute** strike is the one that cannot be
    placed, and it is refused by name rather than quietly read as a ratio.

    The answer carries **both readings of the one point**: the strike, and the
    ``delta`` there under the pair's own convention.  A strike and a delta are
    two ways of naming the same place on the smile and a desk has whichever
    of them the market gave it, so the card takes either and reports the
    other.  The delta is read at ``F = 1`` because it is a function of
    moneyness alone, which is what lets it come back for a pair the feed does
    not quote at all; a request that did not name a wing is answered on the
    call, which is ``resolve_strike``'s rule for a bare ``25d`` said once more.
    """
    surface = book[pair]
    dates = leg_dates(book, pair, expiry)
    expiry_dt = expiry_datetime(book, pair, expiry)
    slice_ = surface.slice_at(expiry_dt, method, cut)
    t = slice_.t
    level = book.market_level_for(pair, expiry_dt.date())
    spec = parse_strike(strike)
    if forward is not None and float(forward) <= 0:
        raise ValueError(f"forward must be positive, got {forward!r}")
    if forward is not None:
        # The command line's own override, and the one box the card does not
        # have: `volkit vol --forward` has always taken a level as typed.
        forward, scaled, source = float(forward), True, "typed"
    elif level["feed"]:
        forward, scaled, source = float(level["forward"]), True, "feed"
    elif spec.kind == "absolute":
        raise ValueError(
            f"the feed does not quote {pair.upper()}"
            + ("" if book.feed is None else
               ", and it does not quote both of the legs it could be built from")
            + f", so the strike {spec.text} cannot be placed against the marks, "
              "which are in strike/forward. Load a feed, or ask for ATM or a delta")
    else:
        forward, scaled, source = 1.0, False, "none"

    # `resolve_strike` settles the wing a bare "25d" did not name, so what it
    # hands back says the side is *now* explicit.  Whether the request said it
    # is a different fact and is the one worth reporting.
    side_asked = bool(spec.side_explicit)
    ratio, absolute, spec = resolve_strike(
        surface, strike, slice_, forward, expiry_dt, method=method, cut=cut)
    vol = float(surface.vol(ratio, expiry_dt, method, cut))

    # The other half of the one question: a strike and a delta name the same
    # point on the smile, and the card asks in whichever of the two the desk
    # has to hand.  The delta is read here rather than in the page because the
    # convention is the pair's own (`VolSurface.conv` -- premium adjusted for a
    # USD-base pair) and a browser has no business knowing which.  Delta is a
    # function of moneyness, so it is read at `F = 1`: it comes back for a pair
    # with no feed exactly as it does for one with a level.
    delta_is_call = bool(spec.is_call) if spec.kind == "delta" else True
    delta_pct = float(black.delta(1.0, ratio, vol, t, delta_is_call,
                                  slice_.conv)) * 100.0

    warnings = list(slice_.warnings)
    if scaled:
        warnings += surface.band_check(absolute, forward, method)
    # A time of day survives the box it was typed in; a tenor has none and
    # lands on the one standard date, which is what goes back into the box.
    when = (expiry_dt.date().isoformat() if expiry_dt.hour == expiry_dt.minute == 0
            else expiry_dt.strftime("%Y-%m-%d %H:%M"))
    return {
        "pair": pair.upper(), "expiry": when, "expiry_text": str(expiry),
        "spot_date": dates.spot.isoformat(), "settle": dates.delivery.isoformat(),
        "settle_rule": dates.rule,
        "days": (expiry_dt - book.clock.now).total_seconds() / 86400.0, "t": t,
        "vol": vol * 100.0, "atm_vol": slice_.atm_vol * 100.0,
        "strike": absolute if scaled else None, "strike_ratio": ratio,
        "strike_spec": spec.text, "strike_kind": spec.kind,
        "is_call": spec.is_call if spec.kind == "delta" else None,
        "side_explicit": side_asked,
        "delta": delta_pct, "delta_is_call": delta_is_call,
        "premium_adjusted": bool(surface.conv),
        "delta_kind": slice_.conv.delta_label(),
        "atm_kind": surface.conv.atm_label(t),
        "convention": slice_.conv.describe(),
        "scaled": scaled, "cut": cut, "method": method or surface.method,
        "forward_source": source,
        "spot": float(level["spot"]) if source == "feed" else None,
        "forward": forward if scaled else None,
        "extrapolated": bool(level["extrapolated"]) if source == "feed" else False,
        "derived": bool(level["derived"]) if source == "feed" else False,
        "via": level["via"] if source == "feed" else "",
        "warnings": warnings,
    }


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
    # -- dates and provenance, appended so the positional order above keeps
    #    meaning what it has always meant --
    #: A settlement date stated rather than derived: a broken date, or a trade
    #: settling somewhere the pair's calendar would not have put it.  Blank
    #: takes the calendar's own (``leg_dates``).
    settle: str = ""
    #: Where the levels in ``spot`` / ``forward`` came from, when the caller
    #: knows and this module cannot tell.  The screen fills both boxes from
    #: the feed and then posts what is in them, so a filled box is not
    #: evidence anybody typed it; ``"feed"`` or ``"typed"`` says which, and
    #: blank leaves ``market_source`` to the old inference -- a box left empty
    #: was the feed's, a box with something in it was somebody's.
    spot_source: str = ""
    forward_source: str = ""


@dataclass
class LegResult:
    """Everything the panel shows for one leg."""

    ok: bool
    label: str = ""
    pair: str = ""
    error: str = ""
    expiry: str = ""
    spot_date: str = ""             # where a spot trade dealt today settles
    settle: str = ""                # where this option settles: the spot lag on
    settle_rule: str = ""           # how that date was arrived at
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
    # The premium as paid: the forward premium discounted at the domestic
    # (quote) currency's RATES-tab rate to the premium date.  ``None`` when
    # the tab has no rate for the currency, and ``discounted`` says which.
    premium_pv_dom: float | None = None
    premium_pv_pct_base: float | None = None
    pv_amount: float | None = None
    discounted: bool = False
    df_domestic: float | None = None
    delta_pct: float = 0.0          # in the pair's quoted convention
    delta_kind: str = "forward delta"   # spot or forward, and why
    smile_delta_pct: float = 0.0
    # millions of base the hedge moves by per 1% move in spot, on the smile
    smile_cash_gamma: float = 0.0
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
        return _discounted(book, _price_leg(book, leg))
    except (ValueError, KeyError, ConvergenceError, ZeroDivisionError) as exc:
        return LegResult(ok=False, label=leg.label, pair=leg.pair,
                         error=f"{type(exc).__name__}: {exc}")


def _discounted(book, r: LegResult) -> LegResult:
    """Put the premium as paid beside the forward premium, where a rate allows.

    Every price above is a forward value.  What changes hands is that value
    discounted at the domestic currency's rate from the option's settlement
    to the premium date -- the spot date, so the period is the same one the
    forward itself covers, ``t`` to a day -- and in the base currency that is
    the same money converted at today's spot.  The exotics get the same
    treatment: their price is a forward value per unit of payout and
    discounts the same way.  No rate on the tab is no discounting, said
    rather than assumed: ``premium_pv_*`` stays ``None`` and ``discounted``
    is ``False``.
    """
    if not r.ok:
        return r
    df = book.discount_factor(r.pair[3:6], r.t) if hasattr(book, "discount_factor") else None
    if df is None:
        return r
    r.df_domestic = float(df)
    r.discounted = True
    r.premium_pv_dom = r.premium_dom * df
    r.premium_pv_pct_base = r.premium_pct_base * df
    r.pv_amount = r.premium_amount * df
    return r


def _resolve_market(book, leg: OptionLeg, expiry: date, settle: date | None = None):
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

    Through ``Book.market_level_for``, so the forward is the one to **this
    leg's settlement date** and a cross the feed quotes only through its legs
    fills a blank box here exactly as it scales the marking screen's strike
    axis.  ``via`` names the legs when that happened and is empty when the
    pair was quoted itself.  ``settle`` is that date when the leg stated one
    rather than taking the calendar's.

    **What is priced is still what is in the box**, always.  ``spot_source``
    and ``forward_source`` change no number: they are the screen saying which
    of the levels it filled from the feed and which somebody typed over, which
    is a fact only the screen has -- it fills the boxes and then posts them,
    so by the time a leg arrives here a feed level and a hand-marked one look
    identical.  Without them the row read ``typed`` for every leg on a screen
    that had never been typed into, which is a provenance label that says
    nothing.
    """
    spot = float(leg.spot) if leg.spot else None
    forward = float(leg.forward) if leg.forward else None
    points = None if leg.forward_points is None else float(leg.forward_points)
    pip = float(leg.pip) if leg.pip else None
    used_spot = used_fwd = False
    via = ""
    if spot is None or (forward is None and points is None):
        level = book.market_level_for(leg.pair, expiry, settle)
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
    said_spot = (leg.spot_source or "").strip().lower()
    said_fwd = (leg.forward_source or "").strip().lower()
    # A box the caller says is the feed's is the feed's, whatever is in it; a
    # box that was blank was filled from the feed just above and is the
    # feed's whatever the caller says.  Neither can be talked out of.
    spot_fed = used_spot or said_spot == "feed"
    fwd_fed = used_fwd or said_fwd == "feed"
    source = ("feed" if spot_fed and fwd_fed else
              # The outright box then holds a typed spot plus the feed's own
              # swap points, which is not the feed's outright and should not
              # claim to be.
              "swap from the feed" if fwd_fed and said_fwd == "feed" else
              "spot from the feed" if spot_fed else
              "forward from the feed" if fwd_fed else "typed")
    return spot, forward, pip, source, via


def _price_leg(book, leg: OptionLeg) -> LegResult:
    product = (leg.product or "vanilla").strip().lower()
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}; expected one of {PRODUCTS}")
    surface = book[leg.pair]
    # A stated settlement date moves the date the forward is read on and
    # nothing else: `t` and the slice come off the expiry, which is what the
    # option is worth time on.
    dates = leg_dates(book, leg.pair, leg.expiry, leg.settle)
    expiry = dates.expiry
    expiry_dt = datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC)
    sl = surface.slice_at(expiry_dt, leg.method, leg.cut)
    t = sl.t
    spot, forward, pip, market_source, feed_via = _resolve_market(
        book, leg, expiry, dates.delivery)

    def vol_at(K_abs: float, *, fwd: float = None, shift: float = 0.0) -> float:
        """Smile vol for an absolute strike, optionally on a shifted surface."""
        return float(surface.vol(K_abs / (fwd or forward), expiry_dt, leg.method, leg.cut)) + shift

    common = dict(
        ok=True, label=leg.label or f"{leg.pair} {expiry:%d%b%y}",
        pair=leg.pair, expiry=expiry.strftime("%Y-%m-%d"),
        spot_date=dates.spot.isoformat(), settle=dates.delivery.isoformat(),
        settle_rule=dates.rule,
        days=(expiry_dt - book.clock.now).total_seconds() / 86400.0, t=t,
        spot=spot, forward=forward, atm_vol=sl.atm_vol * 100.0,
        product=product, market_source=market_source,
        feed_used=market_source != "typed", warnings=list(sl.warnings),
    )
    note = settlement_note(book, leg.pair, dates, bool(str(leg.settle or "").strip()))
    if note:
        common["warnings"] = list(common["warnings"]) + [note]
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
            smile_cash_gamma=float("nan"),
            vega_dom=vega, gamma=0.0,
            premium_amount=notional * price, vega_amount=notional * vega,
            delta_amount=notional * delta * spot,
        )

    # ---- strike-based products ------------------------------------------
    ratio, strike, spec = resolve_strike(
        surface, leg.strike, sl, forward, expiry_dt,
        method=leg.method, cut=leg.cut, option_type=leg.option_type)

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
            smile_cash_gamma=float("nan"),
            vega_dom=vega, gamma=0.0,
            premium_amount=notional * price, vega_amount=notional * vega,
            delta_amount=notional * delta * spot,
        )

    # ---- vanilla --------------------------------------------------------
    premium_dom = float(black.price(forward, strike, vol, t, is_call))
    # The slice's convention: spot delta where the pair quotes one and the
    # RATES tab can price it, forward delta otherwise -- and the row says which.
    delta = float(black.delta(forward, strike, vol, t, is_call, sl.conv))
    vega = float(black.vega(forward, strike, vol, t)) / 100.0
    gamma = float(black.gamma(forward, strike, vol, t))
    try:
        # The forward, not spot: it is what the price above was taken off and
        # what the smile's own strike ratio is against.  Spot went in here
        # once, and on a pair with any forward points that is the delta of a
        # different option -- a 3M EURUSD ATM read 44.6.
        smile_delta = surface.smile_delta(forward, strike, expiry_dt, is_call,
                                          leg.method, leg.cut)
    except (ValueError, ConvergenceError):
        smile_delta = float("nan")
    # The gamma the desk quotes: how far the delta hedge moves for a one per
    # cent move in spot, along the smile rather than at a frozen vol, so it is
    # the derivative of the delta on the row above and not of a Black one.
    # Signed with the leg, like the other amounts, so a sold option shows the
    # short gamma it is.
    try:
        smile_gamma = surface.smile_gamma(forward, strike, expiry_dt, is_call,
                                          leg.method, leg.cut)
    except (ValueError, ConvergenceError):
        smile_gamma = float("nan")
    return LegResult(
        **common, strike=strike, strike_ratio=ratio, strike_spec=spec.text,
        is_call=is_call, vol=vol * 100.0, fair_value=premium_dom,
        premium_dom=premium_dom, premium_pct_base=premium_dom / spot * 100.0,
        delta_pct=delta * 100.0, delta_kind=sl.conv.delta_label(),
        smile_delta_pct=smile_delta * 100.0,
        smile_cash_gamma=notional * smile_gamma,
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
        bucket = totals.setdefault(r.pair, {"premium": 0.0, "vega": 0.0, "delta": 0.0,
                                            "pv_premium": 0.0})
        bucket["premium"] += r.premium_amount
        bucket["vega"] += r.vega_amount
        bucket["delta"] += r.delta_amount
        # The premium as paid totals only if every leg on the pair could be
        # discounted; one leg without a rate makes the total None rather
        # than a sum of two different kinds of money.
        if bucket["pv_premium"] is not None:
            bucket["pv_premium"] = (None if r.pv_amount is None
                                    else bucket["pv_premium"] + r.pv_amount)
    return {
        "legs": [r.__dict__ for r in results],
        "totals": totals,
        "errors": sum(1 for r in results if not r.ok),
        "note": ("premiums are forward values; the premium as paid (pv_*) is the same "
                 "discounted at the term currency's RATES-tab rate, None without one"),
    }
