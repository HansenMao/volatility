"""What the swap points say about peg-break risk, as a check on what the wings say.

``banded.py`` calibrates the regime mixture to the *option* market: the ATM
fixes the Beta body, the wings fix the jump specification, and the hazard is
marked because a joint fit is degenerate (§6).  The forward never gets a vote
beyond pinning the mean.  But on a pegged pair the forward is the liquid
instrument and the options are the thin one, so the swap points deserve to be
asked the same question independently.  This module asks it.

The arithmetic is exact, not a heuristic.  Under the mixture the forward *is*
the risk-neutral mean across all three regimes::

    F  =  hold * m  +  b * F * J        hold = e^{-lambda T},  b = 1 - hold
    J  =  w * e^{+j_w}  +  (1 - w) * e^{-j_s}

where ``m`` is the peg-intact mean and ``J`` is the expected break level as a
multiple of the forward.  Solving for ``b`` at a given ``m`` gives a closed
form with no calibration and no vol input at all::

    b  =  (m - F) / (m - F * J)

Two readings of it matter, and they are different questions:

* **The bound.**  ``m`` has to lie inside the band -- that is what makes it a
  peg.  Putting ``m`` at the binding edge gives the *largest* break
  probability the forward can support.  Mark a hazard above it and the model
  refuses to calibrate at that tenor, because the peg-intact body would have
  to sit outside the band to pay for the jump.  This is a genuine ceiling
  derived from the money market, and it is the number to put beside the
  hazard the wings propose.
* **The attribution.**  Holding ``m`` at spot, how much of the gap between the
  forward and spot does the *marked* break regime actually account for?
  ``break_share_of_gap`` answers it as a fraction: near 1 the marked break
  explains the forward, near 0 the forward is the rate differential and the
  break regime is incidental to it, and **negative** means the two point
  opposite ways.

That second reading is the trap this module exists to keep out of the marks.  A
large forward discount and a large break premium look identical in the forward.
USDHKD is the live example: HKD rates run below USD rates, so the forward walks
toward the strong edge as tenor extends, and a mixture forced to match it
responds by shifting the peg-intact body toward 7.75 -- which reads, wrongly, as
a market view that the peg sits low.  It is carry.

**The share is reported rather than the sign, and that is deliberate.**  The
equivalent spot-anchored *probability* is kept too, but it only looks wrong in
one orientation: against a devaluation marking it comes out obviously negative,
while against a revaluation marking the same carry-driven forward produces a
quietly plausible large probability.  On the desk book, where USDHKD is marked
with a net revaluation break, reading the whole 1-year discount as break premium
gives 38% -- which nobody believes, and which no sign test would have flagged.
The share says 13%, so 87% of that discount is carry, in either orientation.

The corollary is the bound above, and it bites from the long end: at the front
the forward is near spot and constrains almost nothing, while at a year it can
pin the hazard hard.  Which tenor binds is reported, because it is the one
worth arguing about.

**Nothing here is built from a rate differential.**  The forward comes from the
feed's traded swap points through ``Book.market_level_for``, the same call the
rest of the screens use, so a forward quoted here and a band edge placed by
``banded`` can never come from different curves.  The differential is
*reported* -- it is the useful summary of the points, and its sign convention
is stated in the output rather than left to the pair's quoting convention --
but it is a diagnostic, never an input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

from .banded import Band, JumpSpec
from .discount import implied_rate, ois_key

#: Below this the anchor and the expected break level coincide and the forward
#: places no constraint on the hazard at all -- the mixture's mean does not
#: depend on the break probability, so any hazard reprices the same forward.
#: It happens when the weak and strong jumps offset exactly, which is a
#: deliberate marking (a break with no expected direction), not an error.
FLAT_BREAK = 1e-9

#: Below this share of the forward's gap from spot, the marked break regime is
#: not what is moving the forward -- the rate differential is -- and the
#: peg-intact body shift that pays for it must not be read as a view on where
#: the peg sits.  Half is the threshold because that is the point at which the
#: break regime stops being the majority explanation; the share itself is
#: always reported, so a desk that disagrees can read the number.
BREAK_SHARE_FLOOR = 0.5

#: A forward this close to an edge, as a share of the band's width, is worth
#: saying out loud: the mixture needs the forward strictly inside the band, so
#: a pair drifting toward an edge on carry alone will stop calibrating at that
#: tenor with no change in anybody's view.
EDGE_WARNING = 0.05

#: The column the HKMA's aggregate balance is pasted into on the history
#: sheet.  It is a Bloomberg heading and is matched on letters and digits
#: alone (``history.extra_key``), so the spelling here is documentation rather
#: than a pattern that has to be reproduced exactly.
AGGREGATE_BALANCE = "BAL CLOS Index"

#: How far back the aggregate balance is read for its own recent move, in
#: **calendar days**.  About a month: the balance is a policy quantity that
#: steps when the HKMA intervenes and is otherwise flat, so a shorter window
#: is mostly zeros and a longer one blurs the intervention that matters.
#: Counted in dates rather than in rows, because a column with gaps in it
#: would otherwise make "21" mean a different length on every pair.
BALANCE_DAYS = 30

#: A reading older than this, against the last date on the sheet, is called
#: stale.  The HKMA publishes the balance every business day, so a gap longer
#: than a week of them means the pull stopped rather than that nothing
#: happened -- and a balance is at its most interesting exactly when it is
#: moving fast, which is when a stale reading does the most damage.
BALANCE_STALE_DAYS = 8


def expected_break_multiple(spec: JumpSpec) -> float:
    """``E[S_T | the peg broke] / F``, the break level as a multiple of the forward.

    Above 1 the marked break is a net devaluation, below 1 a net revaluation,
    and at 1 the two sides offset and the forward says nothing (:data:`FLAT_BREAK`).
    Which side of 1 this falls on decides *which* band edge bounds the hazard,
    so it is computed once here and read everywhere rather than re-derived.
    """
    return (spec.weak_share * math.exp(spec.weak_jump)
            + (1.0 - spec.weak_share) * math.exp(-spec.strong_jump))


def break_share_of_gap(spot: float, forward: float, t: float,
                       spec: JumpSpec) -> float | None:
    """How much of the forward's gap from spot the *marked* break regime explains.

    The companion to the spot-anchored probability, and the more readable of
    the two because it needs no threshold to interpret.  Holding the
    peg-intact mean at spot, the mixture's own forward is
    ``S (1 + b (J - 1))``, so the gap it accounts for is ``S b (J - 1)`` and
    this is that over the gap the market actually shows.

    Near 1 the marked break explains the forward.  Near 0 the forward is the
    rate differential and the break regime is incidental to it.  **Negative**
    means the two point opposite ways -- the marked break would move the
    forward the other side of spot from where it is.

    It is deliberately orientation-free, which the spot-anchored probability
    is not: that number comes out obviously wrong (negative) against a
    devaluation marking and quietly plausible against a revaluation one, so a
    warning keyed on its sign alone stays silent in the case that matters.
    """
    gap = forward - spot
    if not spot or abs(gap) < abs(spot) * 1e-12 or t <= 0:
        return None
    b = 1.0 - math.exp(-spec.hazard * t)
    return spot * b * (expected_break_multiple(spec) - 1.0) / gap


def rate_legs(curves, base: str, quote: str, spot: float, forward: float,
              t: float) -> dict:
    """The two money-market legs behind a forward, and the basis between them.

    The differential this module reports elsewhere is the *net*, read off the
    traded outright.  That is the right number for pricing and the wrong one
    for attribution: a forward walking toward the strong edge because the
    quote currency's rates fell is peg stress, and one walking there because
    the base currency's rose is the Fed.  The net cannot tell them apart.

    So the legs are read separately, and only from what the feed **states**:

    * ``base_rate`` is the anchor's own OIS curve.
    * ``quote_rate`` is the quote currency's stated curve -- HIBOR, for
      USDHKD -- which ``discount.py`` keeps precisely for this and never
      discounts with.
    * ``quote_rate_implied`` is what covered parity off the base curve and the
      traded forward says that rate is, through ``F = S x DF_base / DF_term``.
    * ``basis`` is the first less the second.

    A non-zero basis is not an error: it is the cross-currency basis, and on a
    defended peg it is the funding premium that appears when the balance
    sheet is scarce.  Reporting it is the point -- the implied curve is
    consistent with the forward by construction, so the *stated* one is the
    only place new information can enter.
    """
    out = {"base": base, "quote": quote, "base_rate": None, "quote_rate": None,
           "quote_rate_implied": None, "basis": None, "source": ""}
    ois = dict(getattr(curves, "ois", None) or {})
    missing = [ois_key(c) for c in (base, quote) if not (ois.get(c) and ois[c].points)]
    if t <= 0 or not spot or not forward:
        out["source"] = "no forward to read the legs against"
        return out
    if ois.get(base) and ois[base].points:
        out["base_rate"] = ois[base].rate(t)
        df_base = ois[base].df(t)
        if df_base is not None:
            out["quote_rate_implied"] = implied_rate(spot * df_base / forward, t)
    if ois.get(quote) and ois[quote].points:
        out["quote_rate"] = ois[quote].rate(t)
    if out["quote_rate"] is not None and out["quote_rate_implied"] is not None:
        out["basis"] = out["quote_rate"] - out["quote_rate_implied"]
    out["source"] = ("both legs stated" if not missing else
                     f"the feed has no {' or '.join(missing)} rows, so the legs cannot be split "
                     f"and only the net differential is available")
    return out


def break_probability_for_anchor(forward: float, anchor: float,
                                 spec: JumpSpec) -> float | None:
    """The break probability that puts the peg-intact mean exactly at ``anchor``.

    The closed form from the module docstring.  ``None`` where the marked
    break has no expected direction, since then the forward is the same at
    every hazard and there is no probability to solve for.

    The result is **not** clamped to [0, 1].  A negative answer is the
    informative case -- it says the forward gap runs opposite to the marked
    break direction, so no amount of break risk explains it -- and clamping it
    to zero would hide exactly the reading this module is for.
    """
    j = expected_break_multiple(spec)
    den = anchor - forward * j
    if abs(den) < FLAT_BREAK or abs(j - 1.0) < FLAT_BREAK:
        return None
    return (anchor - forward) / den


def hazard_from_probability(prob: float, t: float) -> float | None:
    """The annual intensity behind ``P(break by t)``.  ``None`` where there is none.

    A probability at or above 1 is certainty, which no finite hazard reaches,
    and a negative one is not a probability at all; both come back as ``None``
    so that a caller cannot plot a number that does not exist.
    """
    if t <= 0 or prob is None or prob < 0.0 or prob >= 1.0:
        return None
    return -math.log1p(-prob) / t


def forward_hazard_bound(band: Band, forward: float, t: float,
                         spec: JumpSpec) -> dict:
    """The largest hazard at ``t`` that the forward alone can support.

    The peg-intact mean must stay inside the band, so the binding edge is the
    one the mean walks toward as break probability rises: the **lower** edge
    for a net devaluation (the break leg pulls the mean up, so the body must
    come down to pay for it) and the upper edge for a net revaluation.

    This is independent of every volatility quote, which is the point of it.
    ``banded._hazard_ceiling`` answers a related but different question -- the
    hazard above which the *at-the-money* can no longer be repriced -- and
    needs the whole smile to do it.  Where the two disagree the tighter one
    is the real constraint, and which one it is says whether the forward or
    the options are the binding market.
    """
    out = {
        "t": float(t), "forward": float(forward), "edge": "", "edge_level": None,
        "probability": None, "hazard": None, "binding": False,
        "multiple": expected_break_multiple(spec), "message": "",
    }
    if not band.lower < forward < band.upper:
        out["message"] = (
            f"the {t:.4f}-year forward of {forward:.5f} is outside the band "
            f"[{band.lower:g}, {band.upper:g}], so the mixture cannot be calibrated at this "
            f"tenor at all and there is no hazard to bound")
        return out
    j = out["multiple"]
    if abs(j - 1.0) < FLAT_BREAK:
        out["message"] = (
            "the marked break has no expected direction, so the forward is the same at every "
            "hazard and places no bound on it; the bound returns as soon as the weak and "
            "strong jumps stop offsetting")
        return out
    edge = band.lower if j > 1.0 else band.upper
    out["edge"] = "lower" if j > 1.0 else "upper"
    out["edge_level"] = float(edge)
    prob = break_probability_for_anchor(forward, edge, spec)
    if prob is None or not 0.0 < prob < 1.0:
        out["message"] = ("no break probability strictly between 0 and 1 puts the peg-intact "
                          "mean on that edge, so the forward does not bound the hazard here")
        return out
    out["probability"] = float(prob)
    out["hazard"] = hazard_from_probability(prob, t)
    out["binding"] = True
    return out


def aggregate_balance(history, pair: str, *, header: str = AGGREGATE_BALANCE) -> dict:
    """The HKMA aggregate balance off the history sheet, and its own recent move.

    This is the state variable the defence actually runs on, and the reason it
    belongs beside a swap-point read-out: the Convertibility Undertaking
    triggers, the HKMA buys the local currency, **the balance drains**, and
    the local rate has to rise -- which is what moves the forward points.  The
    balance leads that chain, so a differential that has started to compress
    means one thing with the balance draining and another with it flat.

    The **level** is not the signal; where the level sits in its own range is,
    which is why a percentile comes back with it.  The absolute figure is a
    number of Hong Kong dollars and tells a reader nothing on its own.

    Read from whichever sheet carries the column -- the pair's own first.  A
    history workbook names its sheets after pairs, and a sheet named for a
    *currency* (``HKD``) is too short for that reader to place, so it is
    skipped before this ever sees it; where that has happened the skipped
    sheets are named in the message rather than left as an empty answer.
    """
    from .history import extra_key

    out = {"header": header, "found": False, "sheet": "", "latest": None, "date": "",
           "as_of": "", "stale_days": None, "stale": False,
           "previous": None, "previous_date": "", "change": None, "change_days": None,
           "days": BALANCE_DAYS, "low": None,
           "high": None, "percentile": None, "observations": 0, "message": ""}
    if history is None:
        out["message"] = ("no history workbook is loaded, so the aggregate balance cannot be "
                          "read; load one (volkit serve --history ...)")
        return out
    key = extra_key(header)
    pairs = dict(getattr(history, "pairs", None) or {})
    order = ([pair.upper()] if pair.upper() in pairs else []) + \
            [p for p in sorted(pairs) if p != pair.upper()]
    hit = next((p for p in order if pairs[p].extras.get(key) is not None), "")
    if not hit:
        skipped = list(getattr(history, "skipped_sheets", None) or [])
        out["message"] = (
            f"no column matching {header!r} on any sheet of the history workbook"
            + (f"; these sheets were skipped because their names are not pairs: "
               f"{', '.join(skipped)}" if skipped else "")
            + ". A sheet named for a currency rather than a pair is not read at all")
        return out

    import numpy as np

    hist = pairs[hit]
    values = hist.extras[key]
    out.update(found=True, sheet=hit, header=hist.extra_names.get(key, header))
    good = np.flatnonzero(np.isfinite(values))
    out["observations"] = int(good.size)
    if not good.size:
        out["message"] = f"the {out['header']!r} column on {hit} holds no numbers"
        return out
    # The most recent row that actually **has** a number, which is not
    # necessarily the last row of the sheet: a balance column commonly runs a
    # few days behind the price columns beside it, and the reading that
    # matters is the last one published rather than a blank cell today.
    last = int(good[-1])
    series = values[good]
    out["latest"] = float(values[last])
    dates = hist.dates
    value_date = dates[last] if last < len(dates) else None
    if value_date is not None:
        out["date"] = value_date.isoformat()
    if dates:
        out["as_of"] = dates[-1].isoformat()
        if value_date is not None:
            out["stale_days"] = int((dates[-1] - value_date).days)
            out["stale"] = out["stale_days"] > BALANCE_STALE_DAYS
    out["low"], out["high"] = float(series.min()), float(series.max())
    out["percentile"] = float((series <= out["latest"]).mean())

    # The move is measured over calendar days, so a column with gaps does not
    # quietly change what the window means: the comparison is the most recent
    # published reading at least ``BALANCE_DAYS`` days before the latest one.
    if value_date is not None:
        cutoff = value_date - timedelta(days=BALANCE_DAYS)
        earlier = [i for i in good if i < last and i < len(dates) and dates[i] <= cutoff]
        if earlier:
            j = int(earlier[-1])
            out["previous"] = float(values[j])
            out["previous_date"] = dates[j].isoformat()
            out["change"] = out["latest"] - out["previous"]
            out["change_days"] = int((value_date - dates[j]).days)
        else:
            out["message"] = (
                f"the column does not reach back {BALANCE_DAYS} days before "
                f"{out['date']}, so the balance's recent move is not measured")
    if out["stale"]:
        stale = (f"the most recent {out['header']!r} reading is {out['date']}, "
                 f"{out['stale_days']} days behind the sheet's own last date "
                 f"({out['as_of']}). The HKMA publishes this every business day, so a gap that "
                 f"long is the pull having stopped rather than the balance having held still; "
                 f"a defence is exactly when a stale reading misleads most")
        out["message"] = (out["message"] + ". " + stale) if out["message"] else stale
    return out


@dataclass(frozen=True)
class CarryRow:
    """One tenor: where the forward sits, and what it permits.

    ``differential`` is ``r_quote - r_base`` continuously compounded -- for
    USDHKD that is HKD less USD -- and is named that way in the output because
    the sign of a swap point is a quoting convention and this is not.
    """

    tenor: str
    t: float
    spot: float | None = None
    forward: float | None = None
    points: float | None = None
    pip: float | None = None
    differential: float | None = None
    base_rate: float | None = None
    quote_rate: float | None = None
    quote_rate_implied: float | None = None
    cip_basis: float | None = None
    position: float | None = None
    pips_to_lower: float | None = None
    pips_to_upper: float | None = None
    atm: float | None = None
    carry_z: float | None = None
    ceiling_hazard: float | None = None
    ceiling_probability: float | None = None
    ceiling_edge: str = ""
    marked_hazard: float | None = None
    marked_probability: float | None = None
    headroom: float | None = None
    consistent: bool | None = None
    spot_anchored_probability: float | None = None
    break_share_of_gap: float | None = None
    carry_dominated: bool | None = None
    extrapolated: bool = False
    derived: bool = False
    message: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def _differential(spot: float, forward: float, t: float) -> float | None:
    """``r_quote - r_base``, continuously compounded, from the traded outright.

    Read off the forward the feed publishes rather than assembled from two
    deposit curves: covered parity does not hold exactly and the basis is a
    price of its own, so a differential built from rates would not reprice the
    forward this module is reasoning about.
    """
    if t <= 0 or spot is None or forward is None or spot <= 0 or forward <= 0:
        return None
    return math.log(forward / spot) / t


def _carry_z(spot: float, forward: float, atm: float, t: float) -> float | None:
    """``ln(F/S) / (sigma sqrt(T))``: the spot strike's moneyness in sigmas.

    The right summary of a swap point, because it self-normalises for tenor --
    drift is linear in ``T`` and diffusion is only ``sqrt(T)``, which is why
    carry dominates a long expiry and not a short one.  Reported here so that
    "the points are 708" becomes a number that can be compared across tenors.

    On a pegged pair it is a *descriptive* statistic and not a probability:
    the lognormal it normalises against is the model this whole module exists
    because the pair does not obey.
    """
    if atm is None or atm <= 0 or t <= 0 or not spot or not forward:
        return None
    return math.log(forward / spot) / (atm * math.sqrt(t))


def peg_carry_panel(book, pair: str, tenors=None, *, cut: str = "NY",
                    history=None) -> dict:
    """The swap points' reading on break risk, tenor by tenor.

    The read-out behind ``volkit peg-carry``, written as one function so a
    figure quoted off a screen can be reproduced in a batch job -- the same
    arrangement as ``banded.band_panel``, which answers the neighbouring
    question from the option market.

    Levels come from ``Book.market_level_for``, so they are forwards to each
    option's own settlement date rather than to a nominal year fraction.  On a
    one-week tenor that is a fifth of the points, and a module whose whole
    subject is where the forward sits inside a 100-pip band cannot afford to
    place it two business days out.
    """
    if pair not in book:
        raise ValueError(f"{pair} is not built in this book")
    surface = book[pair]
    band = getattr(surface, "band", None)
    treatment = surface.band_treatment
    out = {
        "pair": pair,
        "cut": cut,
        "has_band": band is not None,
        "band": None,
        "base": "", "quote": "",
        "marked": treatment.describe(),
        "rates": "",
        "aggregate_balance": aggregate_balance(history, pair),
        "rows": [],
        "summary": {},
        "warnings": [],
    }
    from .events import pair_legs
    try:
        base, quote = pair_legs(pair)
    except Exception:  # noqa: BLE001 - a pair we cannot split still reports levels
        base, quote = pair[:3], pair[3:6]
    out["base"], out["quote"] = base, quote
    if band is None:
        out["message"] = (
            f"{pair} has no managed band, so there is no peg for the swap points to be a "
            f"second opinion about. Bands are policy and live on the PEG_BANDS tab")
        return out

    effective = treatment.effective_band(band)
    spec = treatment.jump
    out["band"] = {
        "pair": band.pair, "lower": band.lower, "upper": band.upper, "note": band.note,
        "effective_lower": effective.lower, "effective_upper": effective.upper,
        "overridden": (effective.lower, effective.upper) != (band.lower, band.upper),
    }
    out["multiple"] = expected_break_multiple(spec)
    out["direction"] = ("devaluation" if out["multiple"] > 1.0 + FLAT_BREAK else
                        "revaluation" if out["multiple"] < 1.0 - FLAT_BREAK else "none")

    curves = book.discount
    tenors = list(tenors or getattr(surface.atm, "tenor_points", ()) or ())
    rows: list[CarryRow] = []
    for tenor in tenors:
        try:
            t = book.tenor_years(pair, tenor)
        except Exception as exc:  # noqa: BLE001 - one tenor, not the panel
            rows.append(CarryRow(tenor=tenor, t=float("nan"),
                                 message=f"{type(exc).__name__}: {exc}"))
            continue
        rows.append(_row(book, surface, effective, spec, tenor, t, cut,
                         curves, base, quote))
    out["rows"] = [r.as_dict() for r in rows]
    out["rates"] = rate_legs(curves, base, quote, 1.0, 1.0, 1.0)["source"]
    out["summary"] = _summary(rows, out["direction"])
    out["warnings"] = _warnings(rows, effective, out)
    return out


def _row(book, surface, band: Band, spec: JumpSpec, tenor: str, t: float,
         cut: str, curves=None, base: str = "", quote: str = "") -> CarryRow:
    """One tenor of the panel.  A failure here is that tenor's, not the panel's."""
    try:
        expiry = surface.clock.datetime_from_years(t)
        level = book.market_level_for(surface.pair, expiry)
    except Exception as exc:  # noqa: BLE001
        return CarryRow(tenor=tenor, t=t, message=f"{type(exc).__name__}: {exc}")
    if not level["feed"] or level["forward"] is None:
        return CarryRow(tenor=tenor, t=t, message=(
            "no spot / forward feed for this pair, and this module is entirely about where "
            "the forward sits; load one (volkit serve --feed ...) rather than assuming a level"))

    spot, forward = float(level["spot"]), float(level["forward"])
    pip = float(level["pip"] or 1.0)
    row = {
        "tenor": tenor, "t": t, "spot": spot, "forward": forward,
        "points": level["points"], "pip": pip,
        "differential": _differential(spot, forward, t),
        "position": band.position(forward),
        "pips_to_lower": (forward - band.lower) * pip,
        "pips_to_upper": (band.upper - forward) * pip,
        "extrapolated": bool(level["extrapolated"]),
        "derived": bool(level["derived"]),
    }
    legs = rate_legs(curves, base, quote, spot, forward, t)
    row["base_rate"] = legs["base_rate"]
    row["quote_rate"] = legs["quote_rate"]
    row["quote_rate_implied"] = legs["quote_rate_implied"]
    row["cip_basis"] = legs["basis"]
    try:
        row["atm"] = float(surface.atm_vol(expiry, cut))
    except Exception:  # noqa: BLE001 - the carry reading does not depend on it
        row["atm"] = None
    row["carry_z"] = _carry_z(spot, forward, row["atm"], t)

    bound = forward_hazard_bound(band, forward, t, spec)
    row["ceiling_hazard"] = bound["hazard"]
    row["ceiling_probability"] = bound["probability"]
    row["ceiling_edge"] = bound["edge"]
    if not bound["binding"]:
        row["message"] = bound["message"]

    row["marked_hazard"] = float(spec.hazard)
    row["marked_probability"] = 1.0 - math.exp(-spec.hazard * t) if t > 0 else None
    if bound["hazard"] is not None:
        row["headroom"] = bound["hazard"] - spec.hazard
        row["consistent"] = bool(spec.hazard <= bound["hazard"])

    row["spot_anchored_probability"] = break_probability_for_anchor(forward, spot, spec)
    share = break_share_of_gap(spot, forward, t, spec)
    row["break_share_of_gap"] = share
    if share is not None:
        row["carry_dominated"] = bool(share < BREAK_SHARE_FLOOR)
    return CarryRow(**row)


def _summary(rows: list[CarryRow], direction: str) -> dict:
    """The pair-level reading: which tenor binds, and whether the mark clears it.

    The binding tenor is the one with the *tightest* ceiling, which on a pair
    whose carry walks the forward toward an edge is the far end of the curve.
    Naming it is most of the value: it is the one tenor where the forward and
    the wings are actually arguing.
    """
    priced = [r for r in rows if r.ceiling_hazard is not None]
    out = {
        "tenors": len(rows), "priced": len(priced), "direction": direction,
        "binding_tenor": "", "binding_hazard": None, "binding_probability": None,
        "binding_edge": "", "marked_hazard": None, "headroom": None,
        "consistent": None, "carry_dominated": None, "break_share": None,
        "verdict": "",
    }
    if not priced:
        out["verdict"] = ("no tenor has both a feed level and a bounding forward, so the swap "
                          "points have nothing to say about the hazard here")
        return out
    tight = min(priced, key=lambda r: r.ceiling_hazard)
    out.update(binding_tenor=tight.tenor, binding_hazard=tight.ceiling_hazard,
               binding_probability=tight.ceiling_probability,
               binding_edge=tight.ceiling_edge, marked_hazard=tight.marked_hazard,
               headroom=tight.headroom, consistent=tight.consistent,
               break_share=tight.break_share_of_gap)
    dominated = [r for r in rows if r.carry_dominated]
    out["carry_dominated"] = bool(dominated)
    if tight.consistent is False:
        out["verdict"] = (
            f"the marked hazard of {tight.marked_hazard:.3%}/yr is above the "
            f"{tight.ceiling_hazard:.3%}/yr the {tight.tenor} forward supports, so the mixture "
            f"cannot be calibrated there: the peg-intact body would have to sit outside the "
            f"band to pay for the jump. Either the hazard is marked too high or the band is stale")
    else:
        out["verdict"] = (
            f"the {tight.tenor} forward is the binding one and caps the hazard at "
            f"{tight.ceiling_hazard:.3%}/yr "
            f"(P(break by {tight.tenor}) <= {tight.ceiling_probability:.3%}); the marked "
            f"{tight.marked_hazard:.3%}/yr clears it with "
            f"{tight.headroom:.3%}/yr to spare")
    return out


def _warnings(rows: list[CarryRow], band: Band, panel: dict) -> list[str]:
    """What the numbers want said once, rather than on every row."""
    out: list[str] = []
    rates = f"{panel['quote']} / {panel['base']} rate differential"
    body = ("The mixture still has to match the forward, so it pays for it by shifting the "
            "peg-intact body, and that shift is carry rather than fear")
    opposed = [r for r in rows
               if r.break_share_of_gap is not None and r.break_share_of_gap < 0.0]
    if opposed:
        out.append(
            f"{panel['pair']}: at {', '.join(r.tenor for r in opposed)} the forward gap runs "
            f"opposite to the marked {panel['direction']} break, so no break probability "
            f"explains it at all -- that gap is the {rates} and must not be read as a view on "
            f"where the peg sits. {body}")
    thin = [r for r in rows if r.break_share_of_gap is not None
            and 0.0 <= r.break_share_of_gap < BREAK_SHARE_FLOOR]
    if thin:
        worst = min(thin, key=lambda r: r.break_share_of_gap)
        out.append(
            f"{panel['pair']}: at {', '.join(r.tenor for r in thin)} the marked break regime "
            f"accounts for under half the forward's gap from spot -- "
            f"{worst.break_share_of_gap:.1%} of it at {worst.tenor}, so "
            f"{1.0 - worst.break_share_of_gap:.1%} is the {rates}. {body}")
    near = [r for r in rows
            if r.position is not None and min(r.position, 1.0 - r.position) < EDGE_WARNING]
    for r in near:
        edge, pips = (("lower", r.pips_to_lower) if r.position < 0.5
                      else ("upper", r.pips_to_upper))
        out.append(
            f"{panel['pair']}: the {r.tenor} forward is {pips:.1f} pips from the {edge} edge "
            f"({band.lower:g} / {band.upper:g}). The mixture needs the forward strictly inside "
            f"the band, so a further {pips:.1f} pips of carry stops this tenor calibrating "
            f"with nobody's view having changed")
    outside = [r for r in rows if r.forward is not None and not band.contains(r.forward)]
    if outside:
        out.append(
            f"{panel['pair']}: the {', '.join(r.tenor for r in outside)} forward is outside the "
            f"band. Either the peg has moved and PEG_BANDS is stale or the feed is wrong; "
            f"the band model cannot be calibrated to a forward it does not contain")
    if any(r.extrapolated for r in rows):
        out.append(
            f"{panel['pair']}: {', '.join(r.tenor for r in rows if r.extrapolated)} read off "
            f"the end of the swap curve, held flat. A hazard bound from an extrapolated "
            f"forward is a bound on an assumption")
    return out
