"""How wide an FX option's two-way should be: a buffer against the moves it will have to sit through.

The principle (the owner's, 2026-09-24): a market maker's bid-offer is protection.  Half of it
must cover the likely move in the option's own volatility over the time it takes to get out of the
position, judged from how the pieces of the surface have actually moved; and a size the market
cannot absorb at once both lengthens that time and moves the market itself.  So

    half width = Q_p( |move of this option's vol over the hold h| )  +  impact(size)
    h          = the wait for comparable flow  +  size / (share of the flow one can take)

Every input is measured, from one of two places:

* **the Bloomberg history** (quant repo ``data/raw/DATA.pkl``) -- how the at-the-money, the risk
  reversal and the butterfly move, day by day.  A strike's volatility is ``ATM + w_bf * BF +
  w_rr * RR`` (the pillar identity: exactly ``ATM + BF25 + RR25 / 2`` at a 25-delta call), so its
  move on each historical day is that combination of the three days' moves -- their correlation
  and their fat tails come with them, which a sum of three standard deviations would lose.  Each
  day is rescaled to today's regime (filtered historical simulation: a day's move times today's
  EWMA volatility of that series over the EWMA it had then), so a calm year does not set the width
  of a stressed week or the other way round.
* **the DTCC tape** (``tapespread``) -- how the market trades: the notional that goes through a pair
  and tenor per hour, the usual wait between comparable prints, how many effective hours a day's
  volatility arrives in, and how much a large print moves what trades after it.

What is judgement, and is said wherever a width is: the protection quantile ``p`` (how much of the
likely move the half-width covers), the ``share`` of the passing flow a market maker can lay off
into, and the 10-delta wing's move relative to the 25-delta where the history has no 10-delta
series.  The tape's own measured spreads (``tapespread.measure``) are the check on ``p``, not its
target: they are where business printed, much of it dealer to dealer near mid, and a quoted
two-way protects more than that.

When there is less to go on, the width says which rung it stood on:

1. ``history + tape``   the pair's own moves, and its own flow off the tape;
2. ``history``          the pair's own moves; the hold is the median pair's on the tape at that
                        tenor, scaled by the pair's volume where the tape has it at any tenor;
3. ``rule of thumb``    no history of the pair at all: 4.5 bp of premium on USD 100mm, the desk's
                        broker rule, turned into volatility by the option's vega.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import tapespread as tsp

#: RiskMetrics' daily decay: today's volatility of a series, and the one it had on each past day.
EWMA_LAMBDA = 0.94
#: How far back the history reaches for its moves, business days (about ten years).
HISTORY_DAYS = 2520
#: The half-width covers this quantile of the absolute move over the hold.
DEFAULT_P = 0.68
#: The share of the flow through a pair and tenor one market maker can lay a position off into.
DEFAULT_SHARE = 0.20
#: The 10-delta RR and BF move this many times as much as the 25-delta ones, where the history
#: has no 10-delta series (the quant store has none).  A measured number beats it: volkit's own
#: history sheets carry 10-delta columns, and ``wing10_from_history`` reads it off them.
DEFAULT_WING10 = 1.8
RULE_OF_THUMB_BP = 4.5
RULE_OF_THUMB_USD_MM = 100.0

#: The market's tick: the least a vol, an RR or a fly is quoted to move (owner, 2026-09-24:
#: "0.05 ~ 0.1 for vol and rr, longer end less; 0.025 ~ 0.05 for fly").  Rows are (up to this many
#: days, vol / RR / strike tick, fly tick), shortest first.  A two-way is never narrower than one
#: tick and a protective width rounds UP to the grid -- rounding down would quote less protection
#: than the buffer measured.
TICKS = ((45.0, 0.10, 0.05), (float("inf"), 0.05, 0.025))


def tick(instrument: str, T: float) -> float:
    """The tick for an instrument at a tenor, vol points."""
    days = T * 365.0
    for upto, vol_tick, fly_tick in TICKS:
        if days <= upto:
            return fly_tick if instrument == "fly" else vol_tick
    return TICKS[-1][2] if instrument == "fly" else TICKS[-1][1]


def on_ticks(width: float, step: float) -> float:
    """The width rounded up to whole ticks, and never less than one."""
    n = max(1, math.ceil(width / step - 1e-9))
    return n * step

#: The pillars, and a strike's weights on them at call delta 0.10, 0.25, 0.5, 0.75, 0.90.
PILLARS = ("atm", "rr25", "bf25", "rr10", "bf10")
_KNOTS = (0.10, 0.25, 0.50, 0.75, 0.90)
_AT_KNOT = np.array([[1.0, 0.0, 0.0, 0.5, 1.0],
                     [1.0, 0.5, 1.0, 0.0, 0.0],
                     [1.0, 0.0, 0.0, 0.0, 0.0],
                     [1.0, -0.5, 1.0, 0.0, 0.0],
                     [1.0, 0.0, 0.0, -0.5, 1.0]])


def weights(instrument: str, call_delta: float | None = None, wing: int = 25) -> np.ndarray:
    """How much of each pillar one instrument's volatility is (``PILLARS`` order)."""
    w = np.zeros(len(PILLARS))
    if instrument == "atm":
        w[0] = 1.0
    elif instrument in ("rr", "fly"):
        w[PILLARS.index(("rr" if instrument == "rr" else "bf") + str(wing))] = 1.0
    elif instrument == "outright":
        c = float(np.clip(call_delta, _KNOTS[0], _KNOTS[-1]))
        w = np.array([np.interp(c, _KNOTS, _AT_KNOT[:, j]) for j in range(len(PILLARS))])
    else:
        raise ValueError(f"no pillar weights for a {instrument!r}")
    return w


# -- the history: how the pillars move -------------------------------------------------------------
TENORS = (("1W", 7 / 365), ("1M", 1 / 12), ("3M", 0.25), ("6M", 0.5), ("1Y", 1.0))


def _fhs(changes: pd.Series) -> pd.Series:
    """Each day's move rescaled to today's regime: times today's EWMA sd over that day's."""
    d = changes.dropna()
    if len(d) < 30:
        return d
    var = d.pow(2).ewm(alpha=1 - EWMA_LAMBDA, adjust=False).mean()
    sd_then = var.shift(1).bfill().pow(0.5)
    sd_now = float(var.iloc[-1] ** 0.5)
    return d * (sd_now / sd_then.replace(0, np.nan))


@dataclass
class Moves:
    """The pillars' daily moves at one tenor, vol points, as history and as today's regime."""

    pair: str
    tenor: dict[str, str]            # pillar -> the history tenor it was read at
    raw: pd.DataFrame                # daily changes, PILLARS columns (NaN where not held)
    regime: pd.DataFrame             # the same, rescaled to today (filtered historical simulation)
    notes: list[str] = field(default_factory=list)


class History:
    """The pillars' moves for every pair the store holds, read once per pair and tenor."""

    def __init__(self, store: tsp.Store, *, wing10: float = DEFAULT_WING10,
                 asof: pd.Timestamp | None = None):
        self.store = store
        self.wing10 = wing10
        self.asof = asof
        self._cache: dict = {}

    def _series(self, pair: str, kind: str) -> dict[float, pd.Series]:
        if kind == "spot":
            sp = self.store.spot(pair) if self.store is not None else None
            if sp is None:
                return {}
            if self.asof is not None:
                sp = sp[sp.index < self.asof]
            return {0.0: sp.iloc[-HISTORY_DAYS:]}
        if kind == "atm":
            ladder = self.store.atm(pair)
        else:
            ladder = self.store.smile(pair, kind)
        out = {}
        for t, s in ladder.items():
            s = s * 100.0
            if self.asof is not None:
                s = s[s.index < self.asof]
            out[t] = s.iloc[-HISTORY_DAYS:]
        return out

    def level(self, pair: str, T: float) -> float | None:
        """Today's ATM, vol points, at the history tenor nearest ``T``: what a vega is taken at."""
        label = min(TENORS, key=lambda x: abs(math.log(x[1] / max(T, 1e-4))))[0]
        held = getattr(self, "_levels", {}).get((pair, label))
        if held is not None:
            return held
        ladder = self._series(pair, "atm")
        if not ladder:
            return None
        t = min(ladder, key=lambda x: abs(math.log(x / max(T, 1e-4))))
        s = ladder[t].dropna()
        return float(s.iloc[-1]) if len(s) else None

    def spot(self, pair: str) -> pd.Series | None:
        """The pair's daily spot close, for its realized correlation with another."""
        lad = self._series(pair, "spot")
        if lad:
            return next(iter(lad.values()))
        return None

    def moves(self, pair: str, T: float) -> Moves | None:
        key = (pair, min(TENORS, key=lambda x: abs(math.log(x[1] / max(T, 1e-4))))[0])
        if key in self._cache:
            return self._cache[key]
        cols, used, notes = {}, {}, []
        for kind, name in (("atm", "atm"), ("rr", "rr25"), ("bf", "bf25")):
            ladder = self._series(pair, kind)
            if not ladder:
                continue
            t = min(ladder, key=lambda x: abs(math.log(x / max(T, 1e-4))))
            label = next(l for l, y in TENORS if abs(y - t) < 1e-9)
            cols[name] = ladder[t].diff()
            used[name] = label
            if abs(math.log(t / max(T, 1e-4))) > math.log(2.5):
                notes.append(f"{pair}: the {name.upper()} history nearest {T * 365:.0f} days is "
                             f"its {label}; the move is that tenor's")
        if "atm" not in cols:
            self._cache[key] = None
            return None
        raw = pd.DataFrame(cols).dropna(subset=["atm"])
        for name in ("rr25", "bf25"):
            if name not in raw:
                raw[name] = np.nan
                notes.append(f"{pair}: no {name.upper()} history, so a wing's move is its ATM's "
                             f"alone and the width is short by the smile's own risk")
        # The 10-delta pillars move as a multiple of the 25-delta ones: same day, same sign.
        raw["rr10"] = raw["rr25"] * self.wing10
        raw["bf10"] = raw["bf25"] * self.wing10
        regime = pd.DataFrame({c: _fhs(raw[c]) for c in raw}).reindex(raw.index)
        m = Moves(pair, used, raw[list(PILLARS)], regime[list(PILLARS)], notes)
        self._cache[key] = m
        return m


def strike_moves(m: Moves, w: np.ndarray, *, regime: bool = True) -> np.ndarray:
    """The option's own daily vol move on each historical day: the pillars' moves, weighted."""
    X = (m.regime if regime else m.raw).values
    use = np.isfinite(X) | (w == 0)[None, :]
    Xz = np.where(np.isfinite(X), X, 0.0)
    ok = use.all(axis=1) | np.isfinite(X[:, 0])
    return (Xz @ w)[ok & np.isfinite(X[:, 0])]


def move_quantile(daily: np.ndarray, hold_hours: float, p: float, hours_per_day: float) -> float:
    """The p-quantile of the absolute move over the hold, from daily moves.

    Inside a day the move is the day's scaled by the square root of the share of the day's
    variance the hold spans (``hold / hours_per_day``, the tape's own intraday clock).  Past a day,
    overlapping sums of whole days, so a trend in the history is in the tail as it happened.
    """
    daily = daily[np.isfinite(daily)]
    if len(daily) < 30:
        return float("nan")
    days = hold_hours / hours_per_day
    if days <= 1.0:
        return float(np.quantile(np.abs(daily), p) * math.sqrt(max(days, 0.0)))
    k = int(math.ceil(days))
    sums = np.convolve(daily, np.ones(k), mode="valid")
    return float(np.quantile(np.abs(sums), p) * math.sqrt(days / k))


# -- the tape: how the market trades -----------------------------------------------------------------
@dataclass
class TapeFacts:
    """What the DTCC tape says about trading each pair and tenor bucket."""

    flow_usd_mm_per_hour: dict[tuple[str, str], float]   # (pair, tenor) -> median notional / hour
    wait_hours: dict[tuple[str, str], float]             # (pair, tenor) -> median gap, comparable
    hours_per_day: float                                  # effective hours a day's variance takes
    impact: dict = field(default_factory=dict)            # size -> extra move, in daily-move units
    leg_cost_bp: dict = field(default_factory=dict)       # (pair, tenor) -> one leg's cost, bp
    pair_volume: dict[str, float] = field(default_factory=dict)   # pair -> USD mm a day, all tenors
    notes: list[str] = field(default_factory=list)

    def flow(self, pair: str, tenor: str) -> tuple[float | None, str]:
        f = self.flow_usd_mm_per_hour.get((pair, tenor))
        if f:
            return f, "the tape"
        same = [v for (p, t), v in self.flow_usd_mm_per_hour.items() if t == tenor]
        if not same:
            return None, "nothing on the tape at this tenor"
        med = float(np.median(same))
        if pair in self.pair_volume:
            total = np.median(list(self.pair_volume.values()))
            return med * self.pair_volume[pair] / total, \
                "the median pair's at this tenor, scaled by this pair's volume at other tenors"
        return med, "the median pair's at this tenor (this pair is not on the tape)"

    def leg_cost(self, pair: str, tenor: str, delta: str | None = None
                 ) -> tuple[float | None, str]:
        """What crossing the market costs on one option leg, bp of notional, and whose it is.

        ``delta`` ('25c', '10p', ...) asks for a wing leg: that pair's own single options at that
        delta, else every pair's at that tenor and delta, else the at-the-money's legs below.
        """
        if delta and delta != "atm":
            c = self.leg_cost_bp.get((pair, tenor, delta))
            if c:
                return c, f"{pair} {tenor} {delta} single options on the tape"
            c = self.leg_cost_bp.get(("*", tenor, delta))
            if c:
                return c, f"the median pair's {tenor} {delta} single options on the tape"
        c = self.leg_cost_bp.get((pair, tenor))
        if c:
            return c, f"{pair} {tenor} straddles on the tape"
        same = [v for k, v in self.leg_cost_bp.items() if len(k) == 2 and k[1] == tenor]
        if same:
            return float(np.median(same)), f"the median pair's {tenor} straddles on the tape"
        every = [v for k, v in self.leg_cost_bp.items() if len(k) == 2]
        return (float(np.median(every)), "every pair's straddles on the tape") if every else \
            (None, "nothing on the tape")

    def wait(self, pair: str, tenor: str) -> float:
        w = self.wait_hours.get((pair, tenor))
        if w:
            return w
        same = [v for (p, t), v in self.wait_hours.items() if t == tenor]
        return float(np.median(same)) if same else 1.0


#: Currencies quoted with the dollar second (EURUSD); every other is USDXXX.
USD_TERM = ("EUR", "GBP", "AUD", "NZD")


def _usd_mm(obs: pd.DataFrame, store: tsp.Store | None = None) -> np.ndarray:
    """Each print's notional in USD millions: the base notional at the base currency's dollar rate.

    A dollar pair converts off its own spot; a cross off its base currency's dollar leg on the day
    (EURJPY's EUR notional at EURUSD), which ``store`` supplies.  Without it a cross is NaN.
    """
    out = np.where(obs.pair.str[:3] == "USD", obs.N,
                   np.where(obs.pair.str[3:6] == "USD", obs.N * obs.S, np.nan)).astype(float)
    if store is not None:
        cross = ~(obs.pair.str[:3].eq("USD") | obs.pair.str[3:6].eq("USD"))
        for base in obs.loc[cross, "pair"].str[:3].unique():
            leg = f"{base}USD" if base in USD_TERM else f"USD{base}"
            sp = store.spot(leg)
            if sp is None:
                continue
            m = (cross & obs.pair.str[:3].eq(base)).values
            days = pd.DatetimeIndex(obs.ts[m].dt.tz_convert(None).dt.normalize()) \
                - pd.Timedelta(days=1)
            rate = tsp._on_days(sp, days)
            out[m] = obs.N.values[m] * (rate if base in USD_TERM else 1.0 / rate)
    return out / 1e6


def tape_facts(obs: pd.DataFrame, pairs: pd.DataFrame | None = None,
               store: tsp.Store | None = None, measured: pd.DataFrame | None = None) -> TapeFacts:
    """Flow, waits, the intraday clock and size impact, off the inverted tape.

    ``obs`` is ``tapespread.buckets(tapespread.invert(...))``; ``pairs`` is
    ``tapespread.pairs_of`` of it with ``volmove`` joined on (``pair_frame``), used for the clock
    and the impact; without it both take their defaults and say so.
    """
    x = obs.copy()
    x["usd_mm"] = _usd_mm(x, store)
    x["day"] = x.ts.dt.tz_convert(None).dt.normalize()
    daily = x.groupby(["pair", "tenor", "day"]).usd_mm.sum()
    med_daily = daily.groupby(["pair", "tenor"]).median()
    notes = []
    hours = 6.0
    impact = {}
    if pairs is not None and len(pairs) > 200:
        q = pairs[(pairs.kind == "straddle") & pairs.volmove.gt(0)].copy()
        q["z2"] = q.dv2 / q.volmove ** 2
        bins = pd.qcut(q.gap_h, 6, duplicates="drop")
        g = q.groupby(bins, observed=True).agg(gap=("gap_h", "mean"), z2=("z2", _trimmed),
                                               n=("z2", "size"))
        X = np.column_stack([np.ones(len(g)), g.gap.values])
        beta = np.linalg.lstsq(X * np.sqrt(g.n.values)[:, None], g.z2.values *
                               np.sqrt(g.n.values), rcond=None)[0]
        if beta[1] > 0:
            hours = float(np.clip(1.0 / beta[1], 2.0, 24.0))
        notes.append(f"a day's vol move arrives over {hours:.1f} effective hours (the slope of "
                     f"straddle-pair dispersion on the gap, {len(q)} pairs)")
        impact = size_impact(q, x)
    else:
        notes.append("no pairs to read the intraday clock off: 6 hours assumed")
    flow = {k: v / hours for k, v in med_daily.items() if v > 0}
    wait = {}
    if pairs is not None and len(pairs):
        wait = pairs.groupby(["pair", "tenor"]).gap_h.median().to_dict()
    volume = daily.groupby(["pair", "day"]).sum().groupby("pair").median().to_dict()
    # One leg's cost of crossing the market: a straddle's measured spread (tapespread.measure) in
    # premium is spread x two legs' vega, so one leg's is spread x one option's vega.  Kept only
    # where the measurement stood clear of zero by two standard errors.
    legs = {}
    if measured is not None and len(measured):
        st = measured[(measured.kind == "straddle") & measured.spread.gt(0)
                      & (measured.spread > 2 * measured.se.fillna(np.inf))]
        vb = (x[x.kind == "straddle"].assign(vb=x.vega * 100.0)
              .groupby(["pair", "tenor"]).vb.median())
        for _, r in st.iterrows():
            v = vb.get((r.pair, r.tenor))
            if v and np.isfinite(v):
                legs[(r.pair, r.tenor)] = float(r.spread * v)
        notes.append(f"leg cost measured for {len(legs)} pair-tenors off straddle spreads clear of "
                     f"zero; median {np.median(list(legs.values())) if legs else float('nan'):.2f}bp")
        # And a wing leg's own cost, off single options at that delta: priced off their own
        # five-minute spot where the bars reach, which is what makes a single a clean measurement.
        sg = measured[(measured.kind == "single") & measured.spread.gt(0)
                      & (measured.spread > 2 * measured.se.fillna(np.inf))
                      & measured.delta.isin(["25c", "25p", "10c", "10p"])]
        vs = (x[x.kind == "single"].assign(vb=x.vega * 100.0)
              .groupby(["pair", "tenor", "delta"]).vb.median())
        # A pair's own cell stands only where it clears three standard errors; every cell clear
        # of two goes into the pooled median for its tenor and delta (key pair = "*"), which is
        # what the rest read.  Cell by cell the singles are noisy (se ~0.15 vol), and read raw
        # they put a 25-delta wider than the 10-delta beside it.
        pooled: dict = {}
        own = 0
        for _, r in sg.iterrows():
            v = vs.get((r.pair, r.tenor, r.delta))
            if not (v and np.isfinite(v)):
                continue
            cost = float(r.spread * v)
            pooled.setdefault((r.tenor, r.delta), []).append(cost)
            if r.spread > 3 * r.se:
                legs[(r.pair, r.tenor, r.delta)] = cost
                own += 1
        for (tenor, delta), vals in pooled.items():
            if len(vals) >= 2:
                legs[("*", tenor, delta)] = float(np.median(vals))
        notes.append(f"wing leg cost off single options: {own} pair cells clear of three standard "
                     f"errors, {sum(1 for k in legs if k[0] == '*')} pooled tenor-deltas")
    return TapeFacts(flow, wait, hours, impact=impact, leg_cost_bp=legs, pair_volume=volume,
                     notes=notes)


def _trimmed(v: pd.Series) -> float:
    v = v[v <= v.quantile(0.98)]
    return float(v.mean())


def size_impact(q: pd.DataFrame, obs: pd.DataFrame) -> dict:
    """How much further a large print trades from the last comparable one, beyond the gap's drift.

    In units of the bucket's daily vol move, by the later print's size (USD mm).  Positive
    excess over the smallest bucket, at the same gaps, is the concession size costs; it is an
    extra move the half-width must cover, and it is reported with the count behind it because
    tickets above USD 150mm are few and the tape caps the largest.
    """
    q = q.copy()
    q["size"] = np.where(q.pair.str[:3] == "USD", q.N,
                         np.where(q.pair.str[3:6] == "USD", q.N * q.S, np.nan)) / 1e6
    q = q[np.isfinite(q["size"])]
    q["gb"] = pd.qcut(q.gap_h, 4, duplicates="drop")
    edges = [0, 25, 75, 150, 400, float("inf")]
    labels = ["<25", "25-75", "75-150", "150-400", ">400"]
    q["sb"] = pd.cut(q["size"], edges, labels=labels)
    cell = q.groupby(["gb", "sb"], observed=True).z2.agg([_trimmed, "size"])
    base = cell.xs("<25", level="sb")["_trimmed"]
    out = {}
    for lab in labels[1:]:
        try:
            c = cell.xs(lab, level="sb")
        except KeyError:
            continue
        ex = (c["_trimmed"] - base.reindex(c.index)).dropna()
        n = int(c["size"].sum())
        if len(ex) and n >= 30:
            w = c["size"].reindex(ex.index)
            out[lab] = {"excess_z2": float(np.average(ex, weights=w)), "n": n,
                        "lower_mm": edges[labels.index(lab)],
                        "upper_mm": edges[labels.index(lab) + 1]}
    return out


def impact_move(facts: TapeFacts, size_usd_mm: float) -> float:
    """The extra move, in daily-move units, a ticket this size carries; zero where none is seen."""
    if not size_usd_mm or not facts.impact:
        return 0.0
    for e in facts.impact.values():
        if e.get("lower_mm", 0.0) < size_usd_mm <= e["upper_mm"]:
            return math.sqrt(max(e["excess_z2"], 0.0))
    last = max(facts.impact.values(), key=lambda e: e["upper_mm"])
    # past the largest bucket the tape measured, that bucket's impact (the tape caps the largest
    # tickets, so beyond it there is no evidence of more -- and no reason to assume less)
    return math.sqrt(max(last["excess_z2"], 0.0)) if size_usd_mm > last["upper_mm"] else 0.0


def vega_bp(T: float, vol_pts: float, call_delta: float = 0.5) -> float:
    """One option's premium per vol point, bp of notional, at a forward call delta."""
    from scipy.special import ndtri
    d1 = float(ndtri(min(max(call_delta, 1e-6), 1 - 1e-6)))
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * math.sqrt(T) * 100.0


#: Each instrument: how many option legs laying it off crosses, and the call deltas whose vegas
#: its quoted volatility is priced by (the quote's own vega is their mean for an RR, their sum
#: for a fly, one leg's for an outright; a straddle's two ATM legs share the ATM's vega).
LEGS = {"atm": (2, (0.5, 0.5)), "rr": (2, (0.25, 0.75)), "fly": (4, (0.25, 0.75))}


def layoff_cost(instrument: str, T: float, vol_pts: float, leg_bp: float,
                call_delta: float | None = None, wing: int = 25) -> tuple[float, str]:
    """Crossing the market on every leg to get out, in the quote's own vol points.

    The tape's measured cost of one leg, in premium, times the legs, over the premium one vol
    point of the quote is worth.  A fly is four legs (a strangle against a vega-weighted straddle)
    quoted per vol of the strangle's two wings; an RR two legs at the mean of their vegas.
    """
    d = wing / 100.0
    if instrument == "outright":
        v = vega_bp(T, vol_pts, call_delta if call_delta is not None else 0.5)
        return leg_bp / v, f"one leg over its vega {v:.2f}bp"
    n, _ = LEGS[instrument]
    va = vega_bp(T, vol_pts, 0.5)
    vw = vega_bp(T, vol_pts, d)
    if instrument == "atm":
        return n * leg_bp / (2 * va), f"two legs over the straddle's vega {2 * va:.2f}bp"
    if instrument == "rr":
        return n * leg_bp / vw, f"two legs over one wing's vega {vw:.2f}bp"
    return n * leg_bp / (2 * vw), f"four legs over the strangle's vega {2 * vw:.2f}bp"


# -- the width -----------------------------------------------------------------------------------------
@dataclass
class Width:
    """One width, full two-way in vol points, and every part that made it."""

    pair: str
    tenor: str
    instrument: str
    call_delta: float | None
    size_usd_mm: float
    width: float | None = None
    rung: str = ""
    hold_hours: float | None = None
    move: float | None = None          # the p-quantile move over the hold: the half-width's body
    impact: float | None = None        # the size's own extra move
    layoff: float | None = None        # crossing the market on every leg to get out
    raw: float | None = None           # the parts' sum, before the tick grid
    tick: float | None = None          # the tick it was rounded up to
    parts: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.width is None:
            return f"{self.pair} {self.tenor} {self.instrument}: no width"
        return (f"{self.width:.3f} ({self.rung}: lay-off {self.layoff or 0:.3f}, move "
                f"{self.move or 0:.3f}, impact {self.impact or 0:.3f} over {self.hold_hours or 0:.2f}h)")

    def part(self, name, value, unit="vol points", source="", detail=""):
        self.parts.append({"name": name, "value": value, "unit": unit, "source": source,
                           "detail": detail})


def _finish(r: "Width", raw: float, instrument: str, T: float) -> None:
    """Put the width on the tick grid, rounding up, and say by how much on the trace."""
    step = tick(instrument, T)
    r.raw = raw
    r.tick = step
    r.width = on_ticks(raw, step)
    if r.width - raw > 1e-12:
        r.part("tick", r.width - raw, source="the market's tick",
               detail=f"{raw:.3f} rounded up to {r.width / step:.0f} x {step:g}"
                      + ("; one tick is the narrowest two-way" if raw < step else ""))


def tenor_bucket(days: float) -> str:
    return str(pd.cut([days], tsp.TENOR_EDGES, labels=tsp.TENOR_LABELS)[0])


def width(pair: str, T: float, instrument: str = "atm", *, history: History,
          facts: TapeFacts | None, call_delta: float | None = None, wing: int = 25,
          size_usd_mm: float = 50.0, vega_bp: float | None = None, p: float = DEFAULT_P,
          share: float = DEFAULT_SHARE) -> Width:
    """The width for one option: half is the likely move over the hold plus the size's impact.

    ``T`` in years; ``call_delta`` places an outright; ``vega_bp`` (premium per vol point, bp of
    notional) is only needed for the rule-of-thumb rung.
    """
    pair = pair.upper()
    tb = tenor_bucket(T * 365.0)
    r = Width(pair, tb, instrument, call_delta, size_usd_mm)
    w = weights(instrument, call_delta, wing)
    m = history.moves(pair, T)
    if m is None:
        if vega_bp:
            raw = RULE_OF_THUMB_BP / vega_bp
            r.rung = "rule of thumb"
            r.part("rule of thumb", raw, source="the desk's broker rule",
                   detail=f"{RULE_OF_THUMB_BP:g}bp of premium on USD {RULE_OF_THUMB_USD_MM:g}mm "
                          f"over a vega of {vega_bp:.2f}bp per vol")
            r.notes.append(f"{pair} has no volatility history in the store")
            _finish(r, raw, instrument, T)
        else:
            r.notes.append(f"{pair} has no volatility history and no vega was given")
        return r
    r.notes.extend(m.notes)
    hours_per_day = facts.hours_per_day if facts else 6.0
    # the hold: the wait for comparable flow, plus the time the size takes to lay off
    if facts is not None:
        flow, flow_src = facts.flow(pair, tb)
        wait = facts.wait(pair, tb)
        r.rung = "history + tape" if flow_src == "the tape" else "history"
    else:
        flow, flow_src, wait = None, "no tape", 1.0
        r.rung = "history"
    lay_h = (size_usd_mm / (share * flow)) if flow else 0.0
    hold = wait + lay_h
    # nothing is held past its expiry: a thin market's long hold ends when the option does
    life = T * 252.0 * (facts.hours_per_day if facts else 6.0)
    if hold > life:
        r.notes.append(f"the hold of {hold:.1f}h is longer than the option's life; held to expiry "
                       f"({life:.1f}h)")
        hold = life
    r.hold_hours = hold
    r.part("hold", hold, unit="hours", source=flow_src,
           detail=f"{wait:.2f}h wait for comparable flow + USD {size_usd_mm:g}mm at "
                  f"{share:.0%} of {flow:.0f}mm/h" if flow else f"{wait:.2f}h wait")
    # crossing the market on every leg to get out: the tape's cost per leg, in this quote's vol
    lay = 0.0
    if facts is not None:
        # the leg is the strike's own delta bucket for an outright, the wing for an RR or a fly
        if instrument == "outright" and call_delta is not None:
            bucket = str(pd.cut([call_delta], tsp.DELTA_EDGES, labels=tsp.DELTA_LABELS)[0])
        elif instrument in ("rr", "fly"):
            bucket = f"{wing}c"
        else:
            bucket = None
        leg_bp, leg_src = facts.leg_cost(pair, tb, bucket)
        if instrument in ("rr", "fly") and wing == 10:
            # a 10-delta RR or fly is never cheaper a leg to lay off than its 25-delta one: the
            # tails' costs are read short on the tape (one-way flow), the 25-delta's are not
            inner, inner_src = facts.leg_cost(pair, tb, "25c")
            if inner and (not leg_bp or inner > leg_bp):
                leg_bp, leg_src = inner, inner_src + " (the 25-delta's, which a 10-delta is " \
                                                     "never cheaper than)"
        level = history.level(pair, T)
        if leg_bp and level:
            lay, how = layoff_cost(instrument, T, level, leg_bp, call_delta, wing)
            # A wing is never cheaper to get out of, in vol, than a strike between it and the
            # money on the same side.  The tape's wing measurements are lower bounds -- flow in
            # the tails is one-way, so consecutive prints sit on one side and the bounce reads
            # short -- and a lower bound must not make a 10-delta narrower than the 25-delta.
            if instrument == "outright" and call_delta is not None and bucket in (
                    "10c", "25c", "25p", "10p"):
                inner = {"10c": (("25c", 0.25), ("atm", 0.5)), "25c": (("atm", 0.5),),
                         "10p": (("25p", 0.75), ("atm", 0.5)), "25p": (("atm", 0.5),)}[bucket]
                for b_in, c_in in inner:
                    lb, _ = facts.leg_cost(pair, tb, b_in)
                    if lb:
                        floor, _ = layoff_cost("outright", T, level, lb, c_in)
                        if floor > lay:
                            how += f"; held at the {b_in} strike's {floor:.3f}, which it is " \
                                   f"never cheaper than"
                            lay = floor
            r.part("lay-off cost", lay, source=leg_src,
                   detail=f"{leg_bp:.2f}bp a leg; {how}")
    daily = strike_moves(m, w)
    body = move_quantile(daily, hold, p, hours_per_day)
    r.move = body
    r.part("move over the hold", body, source=f"{pair} {', '.join(f'{k} {v}' for k, v in m.tenor.items())}"
           f" history, today's regime", detail=f"{p:.0%} quantile of |move| over {hold:.2f}h "
           f"({hold / hours_per_day:.2f} of a {hours_per_day:.1f}h trading day)")
    imp = 0.0
    if facts is not None:
        day_sd = float(np.sqrt(np.mean(daily ** 2))) if len(daily) else 0.0
        imp = impact_move(facts, size_usd_mm) * day_sd
    r.impact = imp
    r.part("size impact", imp, source="the tape",
           detail="the extra distance tickets of this size trade from the last comparable one")
    r.layoff = lay
    _finish(r, lay + 2.0 * (body + imp), instrument, T)
    return r


def pair_frame(obs: pd.DataFrame, history: History, *, max_gap: float = tsp.MAX_GAP) -> pd.DataFrame:
    """Comparable print pairs (``tapespread.pairs_of``) with the bucket's typical daily move on them.

    ``volmove`` is the root-mean-square daily move of the ATM at the pair's nearest history tenor,
    over the whole history: the unit the intraday clock and the size impact are measured in, so
    pairs and tenors can be pooled.
    """
    p = tsp.pairs_of(obs, max_gap=max_gap)
    p = p.assign(gap_h=p.gap / 3600.0, dv2=p.dv ** 2)
    unit = {}
    for key, T in p.groupby(["pair", "tenor"])["T"].median().items():
        m = history.moves(key[0], T)
        if m is not None:
            a = m.raw["atm"].dropna().values
            unit[key] = float(np.sqrt(np.mean(a * a))) if len(a) else np.nan
    p["volmove"] = [unit.get(k, np.nan) for k in zip(p.pair, p.tenor)]
    return p.dropna(subset=["volmove", "dv2"]).reset_index(drop=True)


# -- a cross: held on its own, or made off its dollar legs --------------------------------------------
#: A 68%-style quantile of |x| for a normal move, used only where the correlation's move is known
#: by its standard deviation alone (the realized fallback): |x| <= q has probability p.
def _normal_abs_quantile(p: float) -> float:
    from scipy.special import ndtri
    return float(ndtri(0.5 + p / 2.0))


def exposures(pair: str, la: str, lb: str) -> tuple[int, int]:
    """How the cross's log price is made of its legs': ``x_c = e_a x_a + e_b x_b``.

    EURJPY = EURUSD x USDJPY is (+1, +1); EURGBP = EURUSD / GBPUSD is (+1, -1); CHFJPY = USDJPY /
    USDCHF is (-1, +1) with its legs in ``dollar_legs`` order (base leg first).  The sign a leg's
    risk reversal enters the cross's with: a base-currency call on the cross is a call on a leg
    written base/USD, and a put on one written USD/base.
    """
    base, term = pair[:3], pair[3:6]
    ea = 1 if la[:3] == base else -1          # BASE/USD -> +1, USD/BASE -> -1
    eb = 1 if lb[3:6] == term else -1          # USD/TERM -> +1, TERM/USD -> -1
    return ea, eb


@dataclass
class Correlation:
    """How the legs' correlation stands and moves, and where that was read."""

    rho: float
    move_quantile: float           # the p-quantile of |the correlation's move| over the hold
    daily_sd: float                # its daily move, today's regime (implied) or measured (realized)
    long_sd: float | None          # its daily move over the whole history, for comparison
    source: str                    # "implied" or "realized"
    detail: str


def implied_correlation(history: History, pair: str, la: str, lb: str, s: int,
                        T: float) -> pd.Series | None:
    """The legs' correlation the three ATM histories imply, day by day, at one history tenor."""
    ladders = [history._series(x, "atm") for x in (pair, la, lb)]
    if not all(ladders):
        return None
    common = set(ladders[0]) & set(ladders[1]) & set(ladders[2])
    if not common:
        return None
    # the nearest tenor all three quote: EURGBP has a 1Y and EURUSD does not
    t = min(common, key=lambda x: abs(math.log(x / max(T, 1e-4))))
    j = pd.concat([lad[t] for lad in ladders], axis=1, keys=["c", "a", "b"]).dropna()
    j = j[(j > 0).all(axis=1)]
    rho = (j.a ** 2 + j.b ** 2 - j.c ** 2) / (2.0 * s * j.a * j.b)
    return rho[rho.between(-1.0, 1.0)]


def realized_correlation(history: History, la: str, lb: str, T: float,
                         lookback: int | None = None) -> tuple[float, float, str] | None:
    """The legs' spot correlation and how much it moves per day, off their spot history alone.

    For a cross with no vol history of its own there is no implied correlation, but there is
    always the two legs' spot.  Their correlation over windows as long as the option is measured
    across the last two years; the spread of those windows' correlations, less what sampling alone
    gives a window that short (``(1 - r^2)^2 / (n - 1.5)``, the correction volkit's
    ``history.realized_corr_vol`` makes), is how much the correlation itself moves over the
    option's life, and that over the square root of the window is its move per day.  Realized,
    not implied: it says how the correlation has moved, not what the market charges for it.
    """
    sa, sb = history.spot(la), history.spot(lb)
    if sa is None or sb is None:
        return None
    r = pd.concat([np.log(sa).diff(), np.log(sb).diff()], axis=1, keys=["a", "b"]).dropna()
    # a month at the least: ten returns give a correlation whose sampling error (~0.3) swamps
    # any movement in it, and the 1W options read off 10-day windows came out at 2.5x the
    # implied correlations' pooled move
    n = int(max(21, round(T * 252)))
    # two years, or eight windows of a long option's length, whichever is longer
    r = r.iloc[-(lookback or max(504, 8 * n)):]
    if len(r) < 3 * n:
        return None
    windows = [r.iloc[i:i + n] for i in range(0, len(r) - n + 1, n)]
    rhos = np.array([np.corrcoef(w.a, w.b)[0, 1] for w in windows])
    rhos = rhos[np.isfinite(rhos)]
    if len(rhos) < 3:
        return None
    noise = float(np.mean((1 - rhos ** 2) ** 2) / (n - 1.5))
    over_life = math.sqrt(max(float(np.var(rhos, ddof=1)) - noise, 0.0))
    # the level over at least a quarter: ten days of a 1W window is a coin toss, not a correlation
    m = max(n, 63)
    rho_now = float(np.corrcoef(r.a.iloc[-m:], r.b.iloc[-m:])[0, 1])
    return rho_now, over_life / math.sqrt(n), (
        f"{len(rhos)} windows of {n} days of the legs' spot, sampling noise taken out")


#: The crosses whose implied correlation can be read off their own and their legs' ATM histories.
POOL_CROSSES = ("EURJPY", "AUDJPY", "CHFJPY", "EURGBP", "EURCHF", "AUDNZD", "AUDCAD", "NZDCAD",
                "EURNOK", "EURSEK", "GBPJPY")


def pooled_corr_move(history: History, T: float) -> float | None:
    """The median daily move of the implied correlation across the crosses that have one.

    A floor for the realized fallback: its noise correction can take a short window's dispersion
    to zero, and a correlation nobody could measure moving has not been shown not to move.
    """
    from .cross import dollar_legs, infer_leg_signs
    key = ("_pool", round(T, 4))
    if key in history._cache:
        return history._cache[key]
    sds = []
    for c in POOL_CROSSES:
        try:
            la, lb = dollar_legs(c)
            sa, sb = infer_leg_signs(c, la, lb)
        except ValueError:
            continue
        rho = implied_correlation(history, c, la, lb, sa * sb, T)
        if rho is not None and len(rho) > 60:
            sds.append(float(rho.diff().dropna().std()))
    out = float(np.median(sds)) if sds else None
    history._cache[key] = out
    return out


def cross_width(pair: str, T: float, *, history: History, facts: TapeFacts | None,
                size_usd_mm: float = 50.0, p: float = DEFAULT_P, share: float = DEFAULT_SHARE
                ) -> tuple[Width, dict]:
    """A cross's at-the-money, both ways, and the cheaper.

    **direct**: the cross's own history and its own flow -- ``width``, exactly as for a dollar
    pair -- where the cross has a vol history.

    **legs**: the vega hedged in the two dollar legs as soon as it is dealt.  In the triangle
    ``sigma_c^2 = sigma_a^2 + sigma_b^2 - 2 s rho sigma_a sigma_b`` the cross moves with each leg
    by ``d sigma_c / d sigma_leg`` and with the correlation by ``d sigma_c / d rho`` (today's
    levels).  So what is paid is each leg's own width at the vega it actually trades -- the leg's
    notional is ``|d sigma_c / d sigma_leg|`` of the cross's -- and what is held is the
    correlation's move, times ``d sigma_c / d rho``, over the cross's own hold (a correlation can
    only be laid off in the cross).  The correlation's move is **explicit**:

    * ``implied``  -- off the three ATM histories, day by day; its daily changes rescaled to today's
      regime like every other move here.  Where the cross has a vol history.
    * ``realized`` -- off the legs' spot alone (``realized_correlation``).  Where it has none, which
      is also where the legs are the only route there is.
    """
    from .cross import dollar_legs, infer_leg_signs

    pair = pair.upper()
    mc = history.moves(pair, T)
    direct = width(pair, T, "atm", history=history, facts=facts, size_usd_mm=size_usd_mm, p=p,
                   share=share) if mc is not None else None
    direct_raw = None if direct is None else (direct.raw if direct.raw is not None
                                              else direct.width)
    info: dict = {"direct": None if direct is None else direct.width, "direct_raw": direct_raw}
    try:
        la, lb = dollar_legs(pair)
        sa_sign, sb_sign = infer_leg_signs(pair, la, lb)
    except ValueError:
        return (direct or Width(pair, tenor_bucket(T * 365), "atm", None, size_usd_mm)), info
    s = sa_sign * sb_sign
    # The legs are read at the tenor the cross's own history was read at: a cross with no 1W
    # series was measured at 1M, and its 1M move against the legs' 1W moves is not a correlation.
    Tc = dict(TENORS).get(mc.tenor["atm"], T) if mc is not None else T
    va, vb = history.level(la, Tc), history.level(lb, Tc)
    if va is None or vb is None:
        info["why"] = "a leg has no ATM history"
        return (direct or Width(pair, tenor_bucket(T * 365), "atm", None, size_usd_mm)), info
    hours_per_day = facts.hours_per_day if facts else 6.0

    # the correlation: implied where the cross has a vol history, realized off the legs' spot else
    corr: Correlation | None = None
    rho_series = implied_correlation(history, pair, la, lb, s, Tc) if mc is not None else None
    if rho_series is not None and len(rho_series) > 60:
        vc = history.level(pair, Tc)
        rho = float(rho_series.iloc[-1])
        d = rho_series.diff().dropna()
        dr = -s * va * vb / vc                   # vol points per unit of correlation
    else:
        real = realized_correlation(history, la, lb, T)
        if real is None:
            info["why"] = "no correlation history, implied or realized"
            return (direct or Width(pair, tenor_bucket(T * 365), "atm", None, size_usd_mm)), info
        rho, sd_daily, how = real
        pool = pooled_corr_move(history, T)
        if pool is not None and sd_daily < pool:
            how += (f"; below the {pool:.4f} a day the implied correlations of crosses with a "
                    f"vol history move at, so held at that -- a correlation measured not to move "
                    f"is sampling noise winning, not a correlation that never moves")
            sd_daily = pool
        vc = math.sqrt(max(va * va + vb * vb - 2.0 * s * rho * va * vb, 1e-8))
        dr = -s * va * vb / vc
        d = None
    a = (va - s * rho * vb) / vc
    b = (vb - s * rho * va) / vc

    # the hold: the cross's own, off its own flow on the tape (the median pair's where it is thin)
    tb = tenor_bucket(T * 365.0)
    if direct is not None:
        hold = direct.hold_hours or 1.0
    else:
        flow, _ = facts.flow(pair, tb) if facts else (None, "")
        wait = facts.wait(pair, tb) if facts else 1.0
        hold = wait + ((size_usd_mm / (share * flow)) if flow else 0.0)
    hold = min(hold, T * 252.0 * hours_per_day)
    if d is not None:
        regime = _fhs(d)
        q = move_quantile(np.abs(dr) * regime.values, hold, p, hours_per_day)
        corr = Correlation(rho, q, float(np.sqrt(np.mean(regime.iloc[-20:] ** 2))),
                           float(d.std()), "implied",
                           f"{len(d)} days of the correlation the three {mc.tenor['atm']} ATMs "
                           f"imply, today's regime")
    else:
        days = hold / hours_per_day
        q = _normal_abs_quantile(p) * abs(dr) * sd_daily * math.sqrt(days)
        corr = Correlation(rho, q, sd_daily, None, "realized", how)

    # the legs, each at the vega it actually trades: |d sigma_c / d sigma_leg| of the size
    wa = width(la, Tc, "atm", history=history, facts=facts, size_usd_mm=abs(a) * size_usd_mm,
               p=p, share=share)
    wb = width(lb, Tc, "atm", history=history, facts=facts, size_usd_mm=abs(b) * size_usd_mm,
               p=p, share=share)
    ra = wa.raw if wa.raw is not None else wa.width
    rb = wb.raw if wb.raw is not None else wb.width
    legs = abs(a) * (ra if ra is not None else np.nan) + abs(b) * (rb if rb is not None
                                                                    else np.nan) + 2.0 * corr.move_quantile
    resid_share = None
    if mc is not None:
        ma, mb = history.moves(la, Tc), history.moves(lb, Tc)
        if ma is not None and mb is not None:
            j = pd.concat({"c": mc.raw["atm"], "a": ma.raw["atm"], "b": mb.raw["atm"]},
                          axis=1).dropna()
            if len(j) > 30 and np.var(j.c.values) > 0:
                resid_share = float(np.var((j.c - a * j.a - b * j.b).values) / np.var(j.c.values))
    info.update({"legs": legs, "rho": rho, "d_sigma_d_legs": (a, b), "d_sigma_d_rho": dr,
                 "leg_pairs": (la, lb), "corr_source": corr.source,
                 "corr_daily_sd": corr.daily_sd, "corr_long_sd": corr.long_sd,
                 "corr_move": corr.move_quantile, "hold_hours": hold,
                 "resid_share": resid_share})
    if np.isfinite(legs) and (direct_raw is None or legs < direct_raw):
        r = Width(pair, tb, "atm", None, size_usd_mm,
                  rung="legs" if corr.source == "implied" else "legs (realized correlation)",
                  hold_hours=hold, move=corr.move_quantile)
        r.part("leg " + la, abs(a) * ra, source=f"{la}'s own width",
               detail=f"d sigma / d {la} {a:+.2f} x {ra:.3f}, at USD {abs(a) * size_usd_mm:.0f}mm")
        r.part("leg " + lb, abs(b) * rb, source=f"{lb}'s own width",
               detail=f"d sigma / d {lb} {b:+.2f} x {rb:.3f}, at USD {abs(b) * size_usd_mm:.0f}mm")
        r.part("correlation over the hold", 2.0 * corr.move_quantile,
               source=f"the {corr.source} correlation",
               detail=f"rho {rho:+.2f}, d sigma / d rho {dr:+.2f}; it moves {corr.daily_sd:.4f} a "
                      f"day ({corr.detail}); {p:.0%} quantile over {hold:.1f}h")
        if direct is not None:
            r.notes.append(f"the cross held on its own reads {direct.width:.3f}; made off its legs "
                           f"is cheaper")
        else:
            r.notes.append(f"{pair} has no vol history of its own: its level is its legs' at the "
                           f"realized correlation, and the legs are the only route")
        _finish(r, legs, "atm", T)
        return r, info
    return direct, info


# -- a cross's smile, off its legs' ------------------------------------------------------------------
def _leg_parts(history: History, pair: str, T: float):
    """Today's triangle for a cross: legs, orientations, partials, levels, correlation and source."""
    from .cross import dollar_legs, infer_leg_signs
    la, lb = dollar_legs(pair)
    sa, sb = infer_leg_signs(pair, la, lb)
    s = sa * sb
    va, vb = history.level(la, T), history.level(lb, T)
    if va is None or vb is None:
        return None
    vc = history.level(pair, T)
    rho = None
    if vc is not None:
        series = implied_correlation(history, pair, la, lb, s, T)
        if series is not None and len(series):
            rho = float(series.iloc[-1])
    if rho is None:
        real = realized_correlation(history, la, lb, T)
        if real is None:
            return None
        rho = real[0]
        vc = math.sqrt(max(va * va + vb * vb - 2.0 * s * rho * va * vb, 1e-8))
    a = (va - s * rho * vb) / vc
    b = (vb - s * rho * va) / vc
    ea, eb = exposures(pair, la, lb)
    return la, lb, ea, eb, a, b, vc, rho


def leg_smile_moves(history: History, pair: str, T: float) -> pd.DataFrame | None:
    """The cross's ATM, RR and BF moves as its legs' would make them, day by day, unscaled.

    ``atm = a dA + b dB``; ``rr = e_a a dRR_a + e_b b dRR_b`` (a leg's risk reversal enters with
    the sign of its exposure in the cross); ``bf = |a| dBF_a + |b| dBF_b`` (convexity adds).  The
    correlation's own moves are not in these -- they are what the ``cross_smile_scales`` factor
    measured on crosses with a history of their own puts back.
    """
    parts = _leg_parts(history, pair, T)
    if parts is None:
        return None
    la, lb, ea, eb, a, b, _, _ = parts
    ma, mb = history.moves(la, T), history.moves(lb, T)
    if ma is None or mb is None:
        return None
    j = pd.concat({"a": ma.raw, "b": mb.raw}, axis=1)
    out = pd.DataFrame({
        "atm": a * j["a"]["atm"] + b * j["b"]["atm"],
        "rr25": ea * a * j["a"]["rr25"] + eb * b * j["b"]["rr25"],
        "bf25": abs(a) * j["a"]["bf25"] + abs(b) * j["b"]["bf25"],
    }).dropna(how="all")
    return out


def cross_smile_scales(history: History, T: float = 1 / 12) -> dict:
    """How much more a cross's ATM, RR and BF move than its legs' make them, and how well they track.

    On every cross with a history of its own: the ratio of the standard deviation of its own
    daily moves to the legs' synthetic ones, and their correlation.  The median ratio is what a
    cross with no history of its own has its leg-made smile scaled by.
    """
    out: dict = {"crosses": {}}
    ratios: dict = {"atm": [], "rr25": [], "bf25": []}
    for c in POOL_CROSSES:
        syn = leg_smile_moves(history, c, T)
        own = history.moves(c, T)
        if syn is None or own is None:
            continue
        row = {}
        for k in ("atm", "rr25", "bf25"):
            j = pd.concat([own.raw[k], syn[k]], axis=1, keys=["own", "syn"]).dropna()
            if len(j) < 60 or j.syn.std() == 0:
                continue
            ratio = float(j.own.std() / j.syn.std())
            row[k] = {"ratio": ratio, "corr": float(j.own.corr(j.syn)), "days": len(j)}
            ratios[k].append(ratio)
        out["crosses"][c] = row
    out["scale"] = {k: float(np.median(v)) if v else None for k, v in ratios.items()}
    return out


def install_leg_smile(history: History, pair: str, T: float, scales: dict) -> bool:
    """Give a cross with no vol history of its own a history built off its legs, and say so.

    Its moves are the legs' (``leg_smile_moves``) times the pooled scales; its level is the legs'
    at their correlation.  Registered on the history so ``width`` reads it like any other pair's.
    """
    if history.moves(pair, T) is not None:
        return False
    parts = _leg_parts(history, pair, T)
    syn = leg_smile_moves(history, pair, T)
    if parts is None or syn is None:
        return False
    sc = scales.get("scale", {}) if scales else {}
    raw = pd.DataFrame(index=syn.index)
    for k in ("atm", "rr25", "bf25"):
        raw[k] = syn[k] * (sc.get(k) or 1.0)
    raw["rr10"] = raw["rr25"] * history.wing10
    raw["bf10"] = raw["bf25"] * history.wing10
    raw = raw[list(PILLARS)]
    regime = pd.DataFrame({c: _fhs(raw[c]) for c in raw}).reindex(raw.index)
    label = min(TENORS, key=lambda x: abs(math.log(x[1] / max(T, 1e-4))))[0]
    m = Moves(pair, {k: f"{label} off the legs" for k in ("atm", "rr25", "bf25")}, raw, regime,
              [f"{pair} has no vol history of its own: its ATM, RR and BF moves are its legs', "
               f"scaled by how much more crosses with a history move than their legs make them "
               f"(ATM x{sc.get('atm') or 1:.2f}, RR x{sc.get('rr25') or 1:.2f}, BF "
               f"x{sc.get('bf25') or 1:.2f})"])
    history._cache[(pair, label)] = m
    history._levels = getattr(history, "_levels", {})
    history._levels[(pair, label)] = parts[6]
    return True


# -- does the buffer cover what it says? ------------------------------------------------------------
def coverage(store: tsp.Store, pair: str, T: float, instrument: str = "atm", *,
             call_delta: float | None = None, p: float = DEFAULT_P, start: str = "2022-01-01",
             wing10: float = DEFAULT_WING10) -> dict:
    """Out of sample: how often the next day's move stayed inside the p-quantile it was given.

    Each month the moves are re-read from the history before it (``History(asof=...)``), today's
    regime taken from that history's end, and every day of the month scored against that month's
    one-day quantile.  A buffer model that is right covers ``p`` of the days; the classical test
    (Kupiec's) is whether the hit count is inside binomial noise of that.
    """
    full = History(store, wing10=wing10)
    m_all = full.moves(pair, T)
    if m_all is None:
        return {"pair": pair, "n": 0}
    w = weights(instrument, call_delta)
    realised = pd.Series(m_all.raw.fillna(0.0).values @ w, index=m_all.raw.index)
    realised = realised[m_all.raw["atm"].notna()]
    months = pd.date_range(start, realised.index[-1], freq="MS")
    hits, n = 0, 0
    for i, mo in enumerate(months):
        nxt = months[i + 1] if i + 1 < len(months) else realised.index[-1] + pd.Timedelta(days=1)
        h = History(store, wing10=wing10, asof=mo)
        m = h.moves(pair, T)
        if m is None:
            continue
        q = move_quantile(strike_moves(m, w), 24.0, p, 24.0)
        # the regime at the month's start is carried through it, as a desk would between re-marks
        day = realised[(realised.index >= mo) & (realised.index < nxt)]
        hits += int((day.abs() <= q).sum())
        n += len(day)
    rate = hits / n if n else float("nan")
    se = math.sqrt(p * (1 - p) / n) if n else float("nan")
    return {"pair": pair, "instrument": instrument, "p": p, "n": n, "covered": rate,
            "z": (rate - p) / se if n else float("nan")}


# -- the study: run once off the quant repo's data, served from one file ----------------------------
STUDY_FILENAME = "bidoffer_study.pkl"


class StoredHistory(History):
    """A ``History`` read from a study file rather than the Bloomberg store."""

    def __init__(self, ladders: dict, *, wing10: float = DEFAULT_WING10):
        self.ladders = ladders             # {(pair, kind): {years: Series of vol points}}
        self.store = None
        self.wing10 = wing10
        self.asof = None
        self._cache = {}

    def _series(self, pair: str, kind: str) -> dict[float, pd.Series]:
        return self.ladders.get((pair.upper(), kind), {})


@dataclass
class Study:
    """Everything a width needs, measured once: the tape's facts and the pillars' histories."""

    facts: TapeFacts
    ladders: dict
    measured: pd.DataFrame
    meta: dict

    def history(self, wing10: float | None = None) -> StoredHistory:
        return StoredHistory(self.ladders, wing10=wing10 or self.meta.get("wing10",
                                                                           DEFAULT_WING10))

    def save(self, path) -> str:
        import pickle
        with open(path, "wb") as f:
            pickle.dump({"facts": self.facts, "ladders": self.ladders, "measured": self.measured,
                         "meta": self.meta}, f, protocol=4)
        return str(path)

    @classmethod
    def load(cls, path) -> "Study":
        import pickle
        with open(path, "rb") as f:
            d = pickle.load(f)
        return cls(d["facts"], d["ladders"], d["measured"], d["meta"])


def run_study(*, tape=tsp.DEFAULT_TAPE, store=tsp.DEFAULT_STORE, bars=tsp.DEFAULT_BARS,
              since: str | None = None, boot: int = 100, log=print) -> Study:
    """Measure the tape and extract the histories: the one slow step, run when the tape has grown."""
    import datetime as _dt
    st = tsp.Store(store)
    prints = tsp.load_prints(tape, since=since)
    trades, merged = tsp.merge_pieces(prints)
    obs = tsp.buckets(tsp.invert(tsp.packages(trades), st, bars=bars))
    log(f"tape: {len(prints)} live vanilla prints, {merged} merged as pieces of one trade, "
        f"{len(obs)} inverted ({(obs.spot_source == 'bar').mean():.0%} on an intraday spot)")
    measured = tsp.measure(obs, boot=boot)
    hist = History(st)
    pf = pair_frame(obs, hist)
    facts = tape_facts(obs, pf, store=st, measured=measured)
    for n in facts.notes:
        log("  " + n)
    ladders = {}
    pairs = sorted({k.split()[0][:6] for k in st.tabs
                    if len(k.split()[0]) > 6 and k.split()[0][6:7] == "V"
                    and k.split()[0][:6].isalpha()})
    for pair in pairs:
        for kind in ("atm", "rr", "bf"):
            lad = hist._series(pair, kind)
            if lad:
                ladders[(pair, kind)] = {t: s.astype("float32") for t, s in lad.items()}
    # every dollar pair's spot, for a cross's realized correlation where it has no vol history
    for tab in st.tabs:
        name = tab.split()[0]
        if tab == f"{name} Curncy" and len(name) == 6 and name.isalpha() and "USD" in (
                name[:3], name[3:]):
            sp = st.spot(name)
            if sp is not None and len(sp) > 300:
                ladders[(name, "spot")] = {0.0: sp.iloc[-HISTORY_DAYS:].astype("float64")}
    meta = {"built": _dt.datetime.now().isoformat(timespec="seconds"), "tape": str(tape),
            "store": str(store), "bars": str(bars), "prints": len(prints), "merged": merged,
            "observations": len(obs), "pairs_with_history": len(pairs),
            "tape_first": str(prints.ts.min())[:10], "tape_last": str(prints.ts.max())[:10],
            "wing10": DEFAULT_WING10,
            "median_ticket_usd_mm": float(np.nanmedian(_usd_mm(obs, st))),
            # What the tape itself says the at-the-money is, pair by tenor bucket, over its last
            # twenty trading days: the level a vega is taken at for a pair the store has no
            # history of (the rule-of-thumb rung).
            "tape_atm": _tape_levels(obs),
            # how far a cross's own ATM / RR / BF moves exceed its legs' making of them, measured
            # on the crosses with a history of their own; what a cross with none is scaled by
            "cross_smile": cross_smile_scales(StoredHistory(ladders))}
    return Study(facts, ladders, measured, meta)


def _tape_levels(obs: pd.DataFrame, days: int = 20) -> dict:
    x = obs[obs.kind == "straddle"].copy()
    x["day"] = x.ts.dt.tz_convert(None).dt.normalize()
    out = {}
    for (pair, tenor), g in x.groupby(["pair", "tenor"]):
        recent = g[g.day >= g.day.max() - pd.Timedelta(days=int(days * 1.4))]
        out[f"{pair} {tenor}"] = float(recent.vol.median())
    return out


def direct_raw_of(info: dict) -> float:
    return info["direct_raw"] if info.get("direct_raw") is not None else info["direct"]


def quote_width(study: Study, pair: str, T: float, instrument: str = "atm", *,
                call_delta: float | None = None, wing: int = 25, size_usd_mm: float | None = None,
                vega_bp_hint: float | None = None, p: float = DEFAULT_P,
                share: float = DEFAULT_SHARE) -> Width:
    """The one entry point a screen asks: a width for any pair, tenor, strike and size.

    A cross's at-the-money takes the cheaper of its two routes (``cross_width``); every other
    instrument is read on the pair's own history.  ``size_usd_mm`` None is the tape's median
    ticket; ``vega_bp_hint`` is only read by the rule-of-thumb rung.
    """
    from .cross import is_cross
    pair = pair.upper()
    hist = study.history()
    size = size_usd_mm if size_usd_mm else study.meta.get("median_ticket_usd_mm", 30.0)
    if vega_bp_hint is None and hist.level(pair, T) is None:
        level = study.meta.get("tape_atm", {}).get(f"{pair} {tenor_bucket(T * 365.0)}")
        if level:
            cd = call_delta if instrument == "outright" else 0.5
            one = vega_bp(T, level, cd if cd is not None else 0.5)
            vega_bp_hint = {"atm": 2 * one, "rr": vega_bp(T, level, 0.25),
                            "fly": 2 * vega_bp(T, level, 0.25)}.get(instrument, one)
    if is_cross(pair) and hist.moves(pair, T) is None:
        # No vol history of its own.  The at-the-money is the leg route alone (explicit, realized
        # correlation); the smile is the legs' smile, scaled as crosses with a history show it
        # should be.  The ATM is routed BEFORE the leg-made history is installed, so that history
        # is never mistaken for the cross's own market.
        routed, info = cross_width(pair, T, history=hist, facts=study.facts, size_usd_mm=size,
                                   p=p, share=share)
        if instrument == "atm" or routed.width is None:
            return routed
        if not install_leg_smile(hist, pair, T, study.meta.get("cross_smile") or {}):
            return routed if instrument == "atm" else Width(pair, tenor_bucket(T * 365),
                                                           instrument, call_delta, size)
        own = width(pair, T, instrument, history=hist, facts=study.facts, call_delta=call_delta,
                    wing=wing, size_usd_mm=size, p=p, share=share)
        own.rung = "legs (realized correlation)"
        if instrument == "outright" and own.raw is not None:
            made = width(pair, T, "atm", history=hist, facts=study.facts, size_usd_mm=size, p=p,
                         share=share)
            own.parts = [q for q in own.parts if q["name"] != "tick"]
            swap = (routed.raw or routed.width) - (made.raw or made.width)
            own.part("at-the-money made off the legs", swap,
                     source="the leg route for the cross's at-the-money",
                     detail=f"the leg-made history's ATM read {made.raw:.3f}; the leg route "
                            f"{routed.raw:.3f}")
            _finish(own, own.raw + swap, "outright", T)
        own.notes.append("the smile's moves are the legs', scaled: a stand-in that tracks a "
                         "cross's own RR well for JPY and dollar-bloc crosses and its fly only "
                         "roughly (claude/bidoffer.md)")
        return own
    if is_cross(pair) and instrument in ("atm", "outright"):
        routed, info = cross_width(pair, T, history=hist, facts=study.facts, size_usd_mm=size,
                                   p=p, share=share)
        if instrument == "atm" or not routed.rung.startswith("legs"):
            if instrument == "atm":
                return routed
        else:
            # An outright on a cross is its at-the-money and its smile.  The at-the-money part is
            # made the cheaper way (off the legs); the smile is the cross's own and is held on its
            # own history.  So the direct outright's ATM share is swapped for the routed one.
            own = width(pair, T, "outright", history=hist, facts=study.facts,
                        call_delta=call_delta, size_usd_mm=size, p=p, share=share)
            if own.width is not None and info.get("direct") is not None:
                own.parts = [p for p in own.parts if p["name"] != "tick"]
                raw = own.raw - direct_raw_of(info) + (routed.raw or routed.width)
                own.rung = "legs"
                own.part("at-the-money made off the legs",
                         (routed.raw or routed.width) - direct_raw_of(info),
                         source="the cheaper route for the cross's at-the-money",
                         detail=f"the cross's own ATM read {info['direct']:.3f}, off its legs "
                                f"{routed.width:.3f}")
                _finish(own, raw, "outright", T)
            return own
    return width(pair, T, instrument, history=hist, facts=study.facts, call_delta=call_delta,
                 wing=wing, size_usd_mm=size, vega_bp=vega_bp_hint, p=p, share=share)


def parse_point(point: str) -> tuple[str, dict]:
    """``atm``, ``25c``, ``10p``, ``rr25``, ``bf10`` -> an instrument and its keywords."""
    s = str(point).strip().lower()
    if s == "atm":
        return "atm", {}
    for prefix, inst in (("rr", "rr"), ("bf", "fly"), ("fly", "fly")):
        if s.startswith(prefix) and s[len(prefix):] in ("10", "25"):
            return inst, {"wing": int(s[len(prefix):])}
    if s[:-1].isdigit() and s[-1] in "cp" and 0 < int(s[:-1]) < 50:
        d = int(s[:-1]) / 100.0
        return "outright", {"call_delta": d if s[-1] == "c" else 1.0 - d}
    raise ValueError(f"{point!r} is not atm, a delta like 25c or 10p, rr25/rr10 or bf25/bf10")


def grid_row(study: Study, pair: str, T: float, tenor: str, point: str, *,
             size_usd_mm: float | None = None, p: float = DEFAULT_P,
             share: float = DEFAULT_SHARE) -> dict:
    """One cell of a grid, as plain data: the width, its rung and every part."""
    inst, kw = parse_point(point)
    try:
        r = quote_width(study, pair, T, inst, size_usd_mm=size_usd_mm, p=p, share=share, **kw)
    except (ValueError, ArithmeticError, KeyError) as exc:
        return {"pair": pair, "tenor": tenor, "point": point, "width": None, "rung": "",
                "parts": [], "notes": [f"{type(exc).__name__}: {exc}"]}
    return {"pair": pair, "tenor": tenor, "point": point, "width": r.width, "rung": r.rung,
            "hold_hours": r.hold_hours, "parts": r.parts, "notes": r.notes}
