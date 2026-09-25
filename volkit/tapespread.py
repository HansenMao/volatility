"""The bid-offer the market actually traded at, measured off the public option tape.

Nothing here reads a width anybody typed.  The only evidence is what traded: every vanilla print
on the DTCC public dissemination tape (the quant repo keeps it, ``data/external/dtcc/``), turned
into a volatility, and the Bloomberg mid history beside it (``data/raw/DATA.pkl``) for the spot,
the forward points and the smile.  ``bidoffer`` explains what this measures; this module only
measures it.

The measurement.  The tape publishes no buyer and no seller, so a spread is not read off one
print.  It is read off **pairs** of prints of comparable options close together in time.  If a
print is the mid plus half the spread on the side it traded, plus whatever the mid has done since
the last one, then for two of them ``gap`` apart

    E[(v2 - v1)^2] = a + b * gap,        a = s^2 / 2 when the sides are independent,

so the spread is the intercept of the squared difference against the gap, ``s = sqrt(2 a)``,
and everything that moves with time -- the vol, the spot, a spot reference that was a few minutes
off -- is in the slope.  That is why every print in a pair is inverted off the **same** spot and
forward reference: an error in the reference is then a move between the two prints, which grows
with the gap and lands in ``b``, never in ``a``.  Intraday bars (``bars``), where there are any,
take the spot move out as well and leave the slope to the volatility alone.

Three kinds of observation, because the spot matters to them differently:

* **straddle** -- a call and a put at one strike, printed together.  Near the money its delta is
  zero, so its volatility barely depends on the spot.  The at-the-money.
* **strangle** -- a call and a put at two strikes, printed together, same notional.  The tape does
  not say whether it was a strangle or a risk reversal, and it does not matter: the two premiums
  are reported as amounts, and one volatility pricing both legs is spot-neutral to first order
  either way.  The wings' level.
* **single** -- one option on its own.  The wing at its own strike, spot-sensitive, and the
  reason the intraday bars are pulled.

Two corrections decide whether the intercept means anything:

* **One trade reported in pieces is not two trades.**  Allocations, prime-brokerage legs and give-
  ups print the same option at the same price per unit notional, seconds to hours apart; out to
  an hour, most repeats on the tape are these.  Read as pairs they would put the spread at zero.
  They are merged (``merge_pieces``).  Two trades at one volatility but different strikes are two
  trades, and are kept.
* **The sides are assumed independent.**  If flow herds -- the next client does what the last one
  did -- pairs straddle the spread less often than half the time and ``sqrt(2a)`` reads low.  The
  measurement says so; it cannot see the sides to correct it.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr

from .feed import pip_divisor

QUANT = Path("~/quant").expanduser()
DEFAULT_TAPE = QUANT / "data" / "external" / "dtcc"
DEFAULT_STORE = QUANT / "data" / "raw" / "DATA.pkl"
DEFAULT_BARS = QUANT / "data" / "external" / "bbg_intraday"

#: Prints of one option at one price per unit notional within this many seconds are one trade.
PIECE_WINDOW = 4 * 3600.0
#: Pairs further apart than this are not used: past a couple of hours the slope is all there is.
MAX_GAP = 2 * 3600.0
#: Tenor buckets, in days to expiry, and their labels.
TENOR_EDGES = (0.0, 10.0, 45.0, 120.0, 400.0, 4000.0)
TENOR_LABELS = ("1W", "1M", "3M", "1Y", ">1Y")
#: Where a single sits on the smile, by call delta: the 25-delta and 10-delta wings each side.
DELTA_EDGES = (0.0, 0.18, 0.35, 0.65, 0.82, 1.0)
DELTA_LABELS = ("10c", "25c", "atm", "25p", "10p")
#: Size buckets, millions of the base currency.
SIZE_EDGES = (0.0, 50.0, 150.0, float("inf"))
SIZE_LABELS = ("<50", "50-150", ">150")


# -- reading ------------------------------------------------------------------------------------
def load_prints(tape=DEFAULT_TAPE, *, since: str | None = None,
                pairs: tuple[str, ...] | None = None) -> pd.DataFrame:
    """Every live vanilla print on the extract, one row each, in UTC.

    Cancelled and superseded reports are gone (``status == live``), and so is a notional DTCC
    marks as a placeholder.  A capped notional is kept, with ``capped`` set: its size is a lower
    bound, which matters to the size bucket and to nothing else here.
    """
    tape = Path(tape)
    files = sorted(tape.glob("fx_options_*.csv.gz"))
    if not files:
        raise FileNotFoundError(f"no fx_options_*.csv.gz under {tape}")
    cols = ["exec_ts", "pair", "side", "product", "strike", "expiry", "tenor_days",
            "notional_base", "capped", "implausible", "premium", "premium_ccy", "status"]
    d = pd.concat([pd.read_csv(f, usecols=cols, low_memory=False) for f in files],
                  ignore_index=True)
    d = d[(d.status == "live") & (d["product"] == "Van")]
    d = d[~d.implausible.fillna(False).astype(bool)]
    d["ts"] = pd.to_datetime(d.exec_ts, utc=True, errors="coerce")
    if since:
        d = d[d.ts >= pd.Timestamp(since, tz="UTC")]
    if pairs:
        d = d[d.pair.isin([p.upper() for p in pairs])]
    d = d.rename(columns={"strike": "K", "notional_base": "N", "premium": "prem",
                          "premium_ccy": "pccy"})
    for c in ("K", "N", "prem", "tenor_days"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d[(d.K > 0) & (d.N > 0) & (d.prem > 0) & (d.tenor_days > 0) & d.ts.notna()]
    d["capped"] = d.capped.fillna(False).astype(bool)
    d["T"] = d.tenor_days / 365.0
    return d[["ts", "pair", "side", "K", "expiry", "T", "N", "capped", "prem", "pccy"]] \
        .sort_values("ts").reset_index(drop=True)


def merge_pieces(prints: pd.DataFrame, window: float = PIECE_WINDOW) -> tuple[pd.DataFrame, int]:
    """One trade reported in pieces becomes one row: same option, same price per unit notional.

    The notional is summed and the first stamp kept.  Returns the rows and how many were merged.
    """
    d = prints.copy()
    d["ppn"] = (d.prem / d.N).round(10)
    d = d.sort_values(["pair", "side", "K", "expiry", "pccy", "ppn", "ts"])
    key = ["pair", "side", "K", "expiry", "pccy", "ppn"]
    gap = d.groupby(key).ts.diff().dt.total_seconds()
    new = gap.isna() | (gap > window)
    d["trade"] = new.cumsum()
    out = d.groupby("trade", sort=False).agg(
        ts=("ts", "first"), pair=("pair", "first"), side=("side", "first"), K=("K", "first"),
        expiry=("expiry", "first"), T=("T", "first"), N=("N", "sum"),
        capped=("capped", "max"), prem=("prem", "sum"), pccy=("pccy", "first"),
        pieces=("ts", "size"))
    return out.sort_values("ts").reset_index(drop=True), int(len(d) - len(out))


# -- the Bloomberg store ------------------------------------------------------------------------
class Store:
    """The daily mids the tape is read against: spot, forward points, ATM / RR / BF."""

    def __init__(self, path=DEFAULT_STORE):
        with open(Path(path), "rb") as f:
            self.tabs: dict = pickle.load(f)
        self.path = str(path)
        self._cache: dict = {}

    def series(self, tab: str, col: str = "PX_LAST") -> pd.Series | None:
        key = (tab, col)
        if key not in self._cache:
            t = self.tabs.get(tab)
            s = None
            if t is not None and col in t:
                s = pd.Series(pd.to_numeric(t[col], errors="coerce").values,
                              index=pd.to_datetime(t["Date"])).sort_index().dropna()
                s = s[~s.index.duplicated(keep="last")]
            self._cache[key] = s
        return self._cache[key]

    def spot(self, pair: str) -> pd.Series | None:
        """The pair's daily close; a cross the store has no tab for is composed from its dollar legs.

        EURAUD = EURUSD / AUDUSD, CADJPY = USDJPY / USDCAD: the same triangle ``cross.exposures``
        states, on the closes, so a cross the desk trades but the pull does not carry still has a
        spot to invert its prints off.
        """
        own = self.series(f"{pair} Curncy")
        if own is not None or "USD" in (pair[:3], pair[3:6]):
            return own
        key = ("_composed", pair)
        if key not in self._cache:
            from .cross import dollar_legs
            try:
                la, lb = dollar_legs(pair)
            except ValueError:
                self._cache[key] = None
                return None
            sa, sb = self.series(f"{la} Curncy"), self.series(f"{lb} Curncy")
            if sa is None or sb is None:
                self._cache[key] = None
                return None
            ea = 1 if la[:3] == pair[:3] else -1
            eb = 1 if lb[3:6] == pair[3:6] else -1
            j = pd.concat([sa, sb], axis=1, keys=["a", "b"]).dropna()
            self._cache[key] = (j.a ** ea) * (j.b ** eb)
        return self._cache[key]

    def forward_points(self, pair: str) -> dict[float, pd.Series]:
        """The outright's points over spot by tenor (years), in price units; empty if none.

        The store keeps them under the non-dollar currency ('EUR1M' is EURUSD, 'JPY1M' USDJPY).
        A cross has none of its own, and none is composed: a forward error is constant through a
        day, and a pair of prints inverted off one reference cancels it.
        """
        ccy = pair[3:6] if pair[:3] == "USD" else pair[:3]
        if "USD" not in (pair[:3], pair[3:6]):
            return {}
        out = {}
        for ten, years in (("1W", 7 / 365), ("1M", 1 / 12), ("3M", 0.25), ("9M", 0.75),
                           ("12M", 1.0)):
            s = self.series(f"{ccy}{ten} Curncy")
            if s is not None:
                out[years] = s / pip_divisor(pair)
        return out

    def atm(self, pair: str) -> dict[float, pd.Series]:
        """ATM implied vol (decimal) by tenor in years."""
        out = {}
        for ten, years in (("1W", 7 / 365), ("1M", 1 / 12), ("3M", 0.25), ("6M", 0.5),
                           ("1Y", 1.0)):
            s = self.series(f"{pair}V{ten} Curncy")
            if s is not None:
                out[years] = s / 100.0
        return out

    def smile(self, pair: str, kind: str) -> dict[float, pd.Series]:
        """25-delta RR or BF (decimal) by tenor in years."""
        out = {}
        for ten, years in (("1W", 7 / 365), ("1M", 1 / 12), ("3M", 0.25), ("6M", 0.5),
                           ("1Y", 1.0)):
            s = self.series(f"{pair}25{'R' if kind == 'rr' else 'B'}{ten} Curncy")
            if s is not None:
                out[years] = s / 100.0
        return out


class HistoryStore(Store):
    """volkit's own historical workbook (``history.load_history``), served as a :class:`Store`.

    The study was written against the quant repo's Bloomberg store, which a desk machine does not
    have; what it has is the workbook ``volkit.cfg`` names as ``history``.  Each of its sheets is
    laid out here under the tab names the store uses -- ``EURUSD Curncy`` for spot,
    ``EURUSDV1M Curncy`` for the ATM, ``EURUSD25R1M``/``25B`` for the 25-delta wings, and the
    forward points under the non-dollar currency (``EUR1M``) in pips -- so every reading of the
    store is unchanged and nothing downstream knows which it was given.

    Only the tenors the store itself reads are carried (``ATM_TENORS``, ``FWD_TENORS``); a sheet
    quoting 2M or 9M vol simply has those columns unused, as the store's tabs would.  A tenor is
    matched by its length in years, so ``12M`` on a sheet is the store's ``1Y``.
    """

    ATM_TENORS = ("1W", "1M", "3M", "6M", "1Y")
    FWD_TENORS = ("1W", "1M", "3M", "9M", "12M")

    def __init__(self, history):
        from .timeutil import tenor_to_years
        self.tabs = {}
        self.path = str(getattr(history, "source", "") or "the historical workbook")
        self._cache = {}
        self.notes: list[str] = []

        def by_years(held: dict, label: str):
            want = tenor_to_years(label)
            for k, v in held.items():
                try:
                    if abs(tenor_to_years(k) - want) < 1e-9:
                        return v
                except ValueError:
                    continue
            return None

        for pair, h in history.pairs.items():
            if not h.dates:
                continue
            dates = pd.to_datetime(pd.Index(h.dates))

            def put(tab, values):
                v = np.asarray(values, dtype=float)
                if v.size == len(dates) and np.isfinite(v).any():
                    self.tabs[tab] = pd.DataFrame({"Date": dates, "PX_LAST": v})

            if h.spot.size:
                put(f"{pair} Curncy", h.spot)
            # ``load_history`` holds every vol as a decimal, whatever unit the sheet was in; a
            # store tab holds vol points, which is what ``atm`` and ``smile`` divide back out.
            for label in self.ATM_TENORS:
                a = by_years(h.atm, label)
                if a is not None:
                    put(f"{pair}V{label} Curncy", np.asarray(a, dtype=float) * 100.0)
                for kind, book in (("R", h.rr), ("B", h.bf)):
                    w = by_years(book.get("25", {}), label)
                    if w is not None:
                        put(f"{pair}25{kind}{label} Curncy", np.asarray(w, dtype=float) * 100.0)
            usd = "USD" in (pair[:3], pair[3:6])
            if usd and h.spot.size and h.forwards:
                ccy = pair[3:6] if pair[:3] == "USD" else pair[:3]
                for label in self.FWD_TENORS:
                    f = by_years(h.forwards, label)
                    if f is not None:
                        put(f"{ccy}{label} Curncy", (f - h.spot) * pip_divisor(pair))
            if not any(k.startswith(f"{pair}V") for k in self.tabs):
                self.notes.append(f"{pair}: no ATM at 1W/1M/3M/6M/1Y on its sheet, so no vol "
                                  f"history for the study")


def _on_days(series: pd.Series | None, days: pd.DatetimeIndex) -> np.ndarray:
    if series is None:
        return np.full(len(days), np.nan)
    uniq = pd.DatetimeIndex(days.unique()).sort_values()
    on = series.reindex(series.index.union(uniq)).ffill().reindex(uniq)
    return on.reindex(days).values


def _term(ladder: dict[float, pd.Series], days: pd.DatetimeIndex, T: np.ndarray,
          variance: bool = False) -> np.ndarray:
    """A term structure read at each row's own tenor: linear in time, or in variance for a vol.

    Flat outside the quoted tenors; a tenor missing on a day is left out of that day's read.
    """
    if not ladder:
        return np.full(len(days), np.nan)
    ts = np.array(sorted(ladder))
    grid = np.column_stack([_on_days(ladder[t], days) for t in ts])
    if variance:
        grid = grid * grid * ts
    T = np.asarray(T, dtype=float)
    out = np.full(len(days), np.nan)
    full = np.all(np.isfinite(grid), axis=1)
    if full.any():
        g, t = grid[full], np.clip(T[full], ts[0], ts[-1])
        j = np.clip(np.searchsorted(ts, t) - 1, 0, len(ts) - 2) if len(ts) > 1 else np.zeros(
            len(t), int)
        if len(ts) > 1:
            w = (t - ts[j]) / (ts[j + 1] - ts[j])
            val = g[np.arange(len(t)), j] * (1 - w) + g[np.arange(len(t)), j + 1] * w
        else:
            val = g[:, 0]
        out[full] = val
    for i in np.flatnonzero(~full):
        ok = np.isfinite(grid[i])
        if ok.any():
            out[i] = np.interp(T[i], ts[ok], grid[i][ok])
    if variance:
        with np.errstate(invalid="ignore", divide="ignore"):
            # below the first tenor the first tenor's vol, rather than its variance over less time
            first = np.where(T < ts[0], np.sqrt(np.maximum(out, 0) / ts[0]), np.nan)
            out = np.where(T < ts[0], first, np.sqrt(np.maximum(out, 0) / T))
    return out


# -- intraday bars ---------------------------------------------------------------------------------
def load_bars(pair: str, bars=DEFAULT_BARS) -> pd.Series | None:
    """Five-minute mid bars for one pair, if the quant store holds any yet."""
    f = Path(bars) / f"{pair}.csv.gz"
    if not f.exists():
        return None
    b = pd.read_csv(f, parse_dates=["time"])
    mid = b[["bid", "ask"]].mean(axis=1, skipna=True)
    s = pd.Series(mid.values, index=pd.DatetimeIndex(b.time)).dropna().sort_index()
    return s if len(s) else None


# -- pricing, vectorised -----------------------------------------------------------------------------
def _black(F, K, v, T, call):
    sq = v * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    c = F * ndtr(d1) - K * ndtr(d2)
    return np.where(call, c, c - F + K)


def _vega(F, K, v, T):
    sq = v * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sq * sq) / sq
    return F * np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) * np.sqrt(T)


def implied(target, F, legs, T, lo=0.005, hi=2.5, iters=80) -> np.ndarray:
    """One volatility pricing a sum of legs, per row: bisection, bracketed, vectorised.

    ``legs`` is a list of ``(K, is_call)`` arrays; the premium is their undiscounted sum.  A row
    whose target lies outside what any volatility in the bracket gives is NaN -- a price below
    intrinsic, or a misread premium -- and never the edge of the bracket.
    """
    lo_a = np.full(len(target), lo)
    hi_a = np.full(len(target), hi)

    def value(v):
        return sum(_black(F, K, v, T, c) for K, c in legs)

    ok = (value(lo_a) <= target) & (value(hi_a) >= target)
    for _ in range(iters):
        mid = 0.5 * (lo_a + hi_a)
        up = value(mid) < target
        lo_a = np.where(up, mid, lo_a)
        hi_a = np.where(up, hi_a, mid)
    return np.where(ok, 0.5 * (lo_a + hi_a), np.nan)


# -- packages ---------------------------------------------------------------------------------------
def packages(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per observation: a straddle, a strangle, or a single.

    Two legs at one stamp on one expiry and one notional, a call and a put: a straddle at one
    strike, a strangle (or risk reversal -- the tape cannot tell, and the premium sum does not care)
    at two with the call above.  Everything else is its legs, each a single.  A leg that is part of
    a larger structure (three legs and up) is a single too: the structure's other legs are not
    comparable to anything, and its own premium is still its own.
    """
    t = trades.copy()
    g = t.groupby(["pair", "ts", "expiry"])
    t["legs"] = g.K.transform("size")
    two = t[t.legs == 2].copy()
    first = two.groupby(["pair", "ts", "expiry"])
    agg = first.agg(sides=("side", lambda s: "".join(sorted(s))), K1=("K", "min"), K2=("K", "max"),
                    N1=("N", "min"), N2=("N", "max"), prem=("prem", "sum"),
                    pccy=("pccy", lambda s: s.iloc[0] if s.nunique() == 1 else None),
                    T=("T", "first"), capped=("capped", "max")).reset_index()
    call_k = first.apply(lambda x: x.loc[x.side == "call", "K"].max()
                         if (x.side == "call").any() else np.nan, include_groups=False).values
    agg["Kc"] = call_k
    pk = agg[(agg.sides == "callput") & (agg.N1 == agg.N2) & agg.pccy.notna()].copy()
    pk["kind"] = np.where(pk.K1 == pk.K2, "straddle",
                          np.where(pk.Kc >= pk.K2, "strangle", "inverted"))
    pk = pk[pk.kind != "inverted"]
    pk = pk.rename(columns={"N1": "N"}).drop(columns=["N2", "sides", "Kc"])
    used = pk.set_index(["pair", "ts", "expiry"]).index
    singles = t[~t.set_index(["pair", "ts", "expiry"]).index.isin(used)].copy()
    singles["kind"] = "single"
    singles["K1"] = singles["K2"] = singles.K
    return pd.concat([pk, singles[["pair", "ts", "expiry", "K1", "K2", "N", "prem", "pccy", "T",
                                   "capped", "kind", "side"]]], ignore_index=True)


def invert(obs: pd.DataFrame, store: Store, *, bars=DEFAULT_BARS) -> pd.DataFrame:
    """Each observation's volatility, and where it sits: call delta, vega, the spot it was read at.

    The spot and forward are the **day's** reference (the NY close before the print, and that
    day's points) unless intraday bars cover the print, in which case the bar at the print is the
    spot.  Either way two prints of one day share a reference, which is what keeps a reference
    error out of the intercept (module docstring).  ``spot_source`` says which it was.
    """
    out = []
    offset = terminal_offset(store, bars)
    for pair, x in obs.groupby("pair"):
        spot = store.spot(pair)
        if spot is None:
            continue
        x = x.copy()
        days = pd.DatetimeIndex(x.ts.dt.tz_convert(None).dt.normalize())
        prev = days - pd.Timedelta(days=1)
        S = _on_days(spot, prev)
        src = np.array(["close"] * len(x), dtype=object)
        b = load_bars(pair, bars)
        if b is not None:
            # The terminal's bar stamps are in its own zone (qcore.data.intraday); they are put on
            # the tape's UTC by the offset that best lines the bars up with the daily closes.
            idx = b.index.tz_localize(None) if b.index.tz is not None else b.index
            idx = (idx - (offset if offset is not None else pd.Timedelta(0))).values
            t = x.ts.dt.tz_convert(None).values
            # the last bar at or before each print, and how old it is; a bar more than a quarter
            # of an hour stale (a gap in the terminal's feed) is not the spot at the print
            pos = np.searchsorted(idx, t, side="right") - 1
            ok = pos >= 0
            at = np.where(ok, b.values[np.clip(pos, 0, len(b) - 1)], np.nan)
            age = np.where(ok, (t - idx[np.clip(pos, 0, len(b) - 1)]) / np.timedelta64(1, "s"),
                           np.inf)
            near = ok & (age < 900) & np.isfinite(at)
            S = np.where(near, at, S)
            src = np.where(near, "bar", src)
        pts = _term(store.forward_points(pair), prev, x["T"].values)
        F = S + np.where(np.isfinite(pts), pts, 0.0)
        base, quote = pair[:3], pair[3:6]
        pq = np.where(x.pccy == quote, x.prem / x.N,
                      np.where(x.pccy == base, x.prem / x.N * S, np.nan))
        T = x["T"].values
        kind = x.kind.values
        call = (x.side.values == "call") if "side" in x else np.zeros(len(x), bool)
        v = np.full(len(x), np.nan)
        pair_legs = np.isin(kind, ("straddle", "strangle"))
        if pair_legs.any():
            m = pair_legs
            v[m] = implied(pq[m], F[m], [(x.K2.values[m], np.ones(m.sum(), bool)),
                                         (x.K1.values[m], np.zeros(m.sum(), bool))], T[m])
        m = kind == "single"
        if m.any():
            v[m] = implied(pq[m], F[m], [(x.K1.values[m], call[m])], T[m])
        Kmid = np.sqrt(x.K1.values * x.K2.values)
        sq = v * np.sqrt(T)
        cd = ndtr((np.log(F / x.K1.values) + 0.5 * sq * sq) / sq)
        x["vol"] = v * 100.0
        x["S"], x["F"] = S, F
        x["call_delta"] = np.where(kind == "straddle", 0.5,
                                   np.where(kind == "strangle",
                                            ndtr((np.log(F / x.K2.values) + 0.5 * sq * sq) / sq),
                                            cd))
        x["vega"] = _vega(F, Kmid, v, T) / np.where(np.isfinite(S) & (S > 0), S, np.nan)
        x["spot_source"] = src
        x["atm_mid"] = 100.0 * _term(store.atm(pair), prev, T, variance=True)
        out.append(x)
    r = pd.concat(out, ignore_index=True) if out else obs.iloc[0:0]
    return r[np.isfinite(r.vol)] if len(r) else r


def terminal_offset(store: "Store", bars=DEFAULT_BARS,
                    probe=("EURUSD", "USDJPY", "GBPUSD", "AUDUSD", "USDCAD")) -> pd.Timedelta | None:
    """The terminal's clock against UTC: one zone for every pair, measured on the majors.

    Each probe pair votes its own best offset (``_bar_offset``); the answer is the vote most of
    them agree on, and None where no pair has both bars and closes.
    """
    # The quant store measures its terminal's zone on every ingest (the newest bar against the
    # workbook's save time); that is exact where the vote below can only tell the hour after the
    # New York close from the one before it.
    man = Path(bars) / "MANIFEST.json"
    if man.exists():
        import json
        from . import paths
        tz = json.loads(paths.read_text(man)).get("tz_offset_hours")
        if tz is not None:
            return pd.Timedelta(hours=float(tz))
    votes = [o for o in (_bar_offset(load_bars(p, bars), store.spot(p)) for p in probe)
             if o is not None]
    if not votes:
        return None
    return pd.Series(votes).mode().iloc[0]


def _ny_close_utc(days: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Each day's 5pm New York, in naive UTC: where Bloomberg's FX daily close sits, DST and all."""
    local = pd.DatetimeIndex(days).tz_localize(None) + pd.Timedelta(hours=17)
    return local.tz_localize("America/New_York").tz_convert("UTC").tz_localize(None)


def _bar_offset(bars: pd.Series, closes: pd.Series) -> pd.Timedelta:
    """Hours to take off the bars' stamps so they line up with the daily closes (UTC).

    Tried over -12h..+12h: the offset at which the bar at each day's 5pm New York is closest to
    that day's close.  Bloomberg's own zone for Dts=S is the terminal's, which nothing here
    assumes; on the desk's VDI it measures +8h (2026-09-24), the same for every pair.
    """
    if bars is None or closes is None:
        return None
    idx = bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
    best, score = 0, float("inf")
    days = closes.index[(closes.index >= idx.min()) & (closes.index <= idx.max())]
    if len(days) < 5:
        return None
    at_close = _ny_close_utc(days)
    for h in range(-12, 13):
        shifted = pd.Series(bars.values, index=idx - pd.Timedelta(hours=h))
        shifted = shifted[~shifted.index.duplicated(keep="last")]
        at = shifted.reindex(shifted.index.union(at_close)).ffill().reindex(at_close).values
        e = np.nanmedian(np.abs(at / closes.reindex(days).values - 1.0))
        if e < score:
            best, score = h, e
    return pd.Timedelta(hours=best)


# -- the measurement --------------------------------------------------------------------------------
def buckets(obs: pd.DataFrame) -> pd.DataFrame:
    x = obs.copy()
    days = x["T"] * 365.0
    x["tenor"] = pd.cut(days, TENOR_EDGES, labels=TENOR_LABELS).astype(str)
    x["delta"] = np.where(x.kind == "straddle", "atm",
                          pd.cut(x.call_delta.clip(0, 1), DELTA_EDGES, labels=DELTA_LABELS)
                          .astype(str))
    x.loc[x.kind == "strangle", "delta"] = np.where(
        x.loc[x.kind == "strangle", "call_delta"] < 0.18, "10", "25")
    x["size"] = pd.cut(x.N / 1e6, SIZE_EDGES, labels=SIZE_LABELS).astype(str)
    return x


def pairs_of(obs: pd.DataFrame, *, max_gap: float = MAX_GAP, by=("pair", "kind", "tenor", "delta"),
             delta_tol: float = 0.03) -> pd.DataFrame:
    """Consecutive comparable observations: one bucket, one expiry, one day, close in delta.

    Pairs of the same day only (one spot reference), within ``max_gap`` seconds.  A single is
    compared with a single on the same side; its smile position must match to ``delta_tol`` of
    call delta, because two strikes a delta apart differ by the smile and not by the spread.
    """
    x = obs.sort_values(list(by) + ["expiry", "ts"]).copy()
    x["day"] = x.ts.dt.tz_convert(None).dt.normalize()
    # Both prints of a pair must stand on the same kind of spot: two off the day's close share
    # one reference (its error is drift), two off their own bars share the market's (the spot is
    # out of the difference altogether), and one of each shares neither.
    if "spot_source" not in x:
        x["spot_source"] = "close"
    keys = list(by) + ["expiry", "day", "spot_source"] + (["side"] if "side" in x else [])
    x["side"] = x.get("side", pd.Series("", index=x.index)).fillna("")
    g = x.groupby(keys, sort=False)
    prev = g[["ts", "vol", "call_delta", "N"]].shift()
    p = x.assign(gap=(x.ts - prev.ts).dt.total_seconds(), dv=x.vol - prev.vol,
                 dd=(x.call_delta - prev.call_delta).abs(), N_prev=prev.N)
    p = p[p.gap.notna() & (p.gap > 0) & (p.gap <= max_gap) & (p.dd <= delta_tol)]
    return p


@dataclass
class Estimate:
    """One bucket's measured spread, full two-way, vol points, with how much stands behind it."""

    spread: float | None
    se: float | None
    intercept: float | None
    slope_per_hour: float | None
    pairs: int
    days: int
    note: str = ""
    extra: dict = field(default_factory=dict)


def _fit(gap_h: np.ndarray, dv2: np.ndarray) -> tuple[float, float]:
    """Intercept and slope of the robust second moment on the gap, over gap quantile bins."""
    nb = int(np.clip(len(gap_h) // 60, 3, 12))
    edges = np.unique(np.quantile(gap_h, np.linspace(0, 1, nb + 1)))
    if len(edges) < 3:
        return float(np.mean(dv2)), 0.0
    xs, ys, ws = [], [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (gap_h >= lo) & (gap_h <= hi)
        if m.sum() < 10:
            continue
        v = dv2[m]
        # A misread premium is a squared error of hundreds; the mean of the bin after the worst
        # two per cent is the second moment of the market, not of the tape's typos.
        cut = np.quantile(v, 0.98)
        xs.append(float(np.mean(gap_h[m])))
        ys.append(float(np.mean(v[v <= cut])))
        ws.append(float(m.sum()))
    if len(xs) < 2:
        return float(np.mean(dv2)), 0.0
    X = np.column_stack([np.ones(len(xs)), xs])
    W = np.diag(ws)
    beta = np.linalg.lstsq(np.sqrt(W) @ X, np.sqrt(W) @ np.array(ys), rcond=None)[0]
    return float(beta[0]), float(beta[1])


def estimate(p: pd.DataFrame, *, boot: int = 200, seed: int = 7, min_pairs: int = 60) -> Estimate:
    """The spread of one bucket from its pairs, with a day-block bootstrap standard error."""
    n = len(p)
    nd = int(p.day.nunique()) if n else 0
    if n < min_pairs:
        return Estimate(None, None, None, None, n, nd, f"{n} pairs; {min_pairs} are needed")
    gap_h = p.gap.values / 3600.0
    dv2 = p.dv.values ** 2
    a, b = _fit(gap_h, dv2)
    rng = np.random.default_rng(seed)
    days = p.day.values
    uniq = np.unique(days)
    by_day = {d: np.flatnonzero(days == d) for d in uniq}
    draws = []
    for _ in range(boot):
        pick = rng.choice(uniq, size=len(uniq), replace=True)
        idx = np.concatenate([by_day[d] for d in pick])
        aa, _ = _fit(gap_h[idx], dv2[idx])
        draws.append(math.sqrt(2 * aa) if aa > 0 else 0.0)
    s = math.sqrt(2 * a) if a > 0 else 0.0
    note = "" if a > 0 else "the intercept is not above zero: no spread distinguishable from drift"
    return Estimate(s, float(np.std(draws)), a, b, n, nd, note)


def measure(obs: pd.DataFrame, *, by=("pair", "kind", "tenor", "delta"), boot: int = 200,
            min_pairs: int = 60, max_gap: float = MAX_GAP) -> pd.DataFrame:
    """Every bucket's measured spread: the table the explanation is fitted to."""
    p = pairs_of(obs, by=by, max_gap=max_gap)
    rows = []
    for key, grp in p.groupby(list(by), sort=True):
        e = estimate(grp, boot=boot, min_pairs=min_pairs)
        rows.append(dict(zip(by, key if isinstance(key, tuple) else (key,))) | {
            "spread": e.spread, "se": e.se, "intercept": e.intercept,
            "slope_per_hour": e.slope_per_hour, "pairs": e.pairs, "days": e.days,
            "median_gap_min": float(grp.gap.median() / 60.0),
            "bar_share": float((grp.spot_source == "bar").mean()) if "spot_source" in grp else 0.0,
            "note": e.note})
    return pd.DataFrame(rows)
