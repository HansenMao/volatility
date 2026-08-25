"""Exchange traded options: fit a SABR curve to a quoted strike/vol table.

The rest of volkit works the way an FX options desk quotes: an at-the-money
volatility, a risk reversal and a market strangle, three numbers that pin down
three SABR parameters exactly.  A listed market does not work that way.  The
exchange publishes a *list of strikes* with a settlement volatility against
each, there is no distinguished at-the-money quote, and the number of points
is whatever happens to be listed -- five on a quiet back month, sixty on the
front.  So the calibration here is a genuine least-squares fit rather than a
three-condition solve, and it needs its own module.

Three things this is for:

* **Fitting.**  ``fit_sabr`` takes N strike/volatility pairs and returns
  SABR parameters, the residual at every point, and the diagnostics that say
  whether the shape could reproduce the quotes at all.
* **Comparing.**  A listed contract usually has an OTC cousin.  When the
  underlying maps to a pair in the book, the same expiry is queried on the
  marked surface and the two are shown side by side at the *same physical
  strikes* -- see ``compare_to_surface`` for why that matters.
* **Reading a paste.**  ``parse_quote_table`` accepts what actually comes out
  of a terminal or a spreadsheet: tabs or commas, headers or none, bid/ask or
  mid, percent or decimal.  Everything it infers is reported, and every line
  it cannot use is returned with the reason.  Nothing is dropped quietly.

Conventions.  Volatilities are decimals everywhere in this module (0.0925, not
9.25); the parser converts.  Strikes are in the *listed* contract's own units.
Time to expiry comes from the book's injected ``Clock``, so a fit and the mark
it is compared against are measured on the same 365.2425-day year.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares, minimize_scalar

from . import black
from .numerics import ConvergenceError
from .sabr import SabrParams, alpha_roots_at_forward, lognormal_vol

# ---------------------------------------------------------------------------
# underlyings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListedUnderlying:
    """A listed contract and how its strikes relate to an FX pair.

    ``invert`` is the whole reason this class exists.  The CME quotes its yen
    contract in USD per JPY, so a strike of 0.006850 on the future is a strike
    of 145.985 on USDJPY.  Lognormal implied volatility is invariant under that
    inversion -- a call on X struck at K and a put on 1/X struck at 1/K carry
    the same Black volatility -- so the *volatilities* compare directly once
    the *strikes* have been mapped.  What does not survive the mapping is the
    smile's orientation: the upper wing of one is the lower wing of the other.
    That is handled by comparing at matched strikes and never by matching
    deltas or flipping a risk-reversal sign by hand.

    ``scale`` multiplies a quoted strike before the mapping, for the venues
    that list yen strikes as ``6850`` rather than ``0.006850``.
    """

    code: str
    name: str
    pair: str | None = None
    invert: bool = False
    scale: float = 1.0
    note: str = ""

    def to_fx(self, strike):
        """Map a listed strike (or array of them) into the FX pair's units."""
        k = np.asarray(strike, dtype=float) * self.scale
        if np.any(k <= 0):
            raise ValueError(
                f"{self.code}: strike {float(np.min(k)):.6g} is not positive after the "
                f"scale factor {self.scale:g}; check the units of the pasted table"
            )
        out = 1.0 / k if self.invert else k
        return float(out) if np.isscalar(strike) or np.ndim(strike) == 0 else out

    def from_fx(self, strike):
        """The inverse of :meth:`to_fx`, for putting book strikes on the listed axis."""
        k = np.asarray(strike, dtype=float)
        if np.any(k <= 0):
            raise ValueError(f"{self.code}: FX strike {float(np.min(k)):.6g} is not positive")
        out = (1.0 / k if self.invert else k) / self.scale
        return float(out) if np.isscalar(strike) or np.ndim(strike) == 0 else out


def _u(code, name, pair=None, invert=False, scale=1.0, note=""):
    return ListedUnderlying(code, name, pair, invert, scale, note)


# The CME quotes every currency future as US dollars per unit of the foreign
# currency, which is the market convention for four of these pairs and the
# reciprocal of it for the other five.  ``invert`` records which.
UNDERLYINGS: dict[str, ListedUnderlying] = {
    u.code: u for u in (
        _u("CUSTOM", "custom / specify the pair yourself"),
        _u("6E", "CME euro future", "EURUSD", False),
        _u("6B", "CME sterling future", "GBPUSD", False),
        _u("6A", "CME Australian dollar future", "AUDUSD", False),
        _u("6N", "CME New Zealand dollar future", "NZDUSD", False),
        _u("6J", "CME yen future", "USDJPY", True,
           note="quoted in USD per JPY; strikes listed as integers need scale 1e-6"),
        _u("6C", "CME Canadian dollar future", "USDCAD", True),
        _u("6S", "CME Swiss franc future", "USDCHF", True),
        _u("6M", "CME Mexican peso future", "USDMXN", True),
        _u("6L", "CME Brazilian real future", "USDBRL", True),
        _u("6Z", "CME South African rand future", "USDZAR", True),
        _u("E7", "CME E-mini euro future", "EURUSD", False),
        _u("J7", "CME E-mini yen future", "USDJPY", True),
    )
}


def resolve_underlying(code: str | None, *, pair: str | None = None,
                       invert: bool | None = None, scale: float | None = None) -> ListedUnderlying:
    """Look a contract up, with per-panel overrides applied on top."""
    key = (code or "CUSTOM").strip().upper()
    base = UNDERLYINGS.get(key)
    if base is None:
        raise ValueError(
            f"unknown listed underlying {code!r}; expected one of "
            f"{', '.join(sorted(UNDERLYINGS))}, or CUSTOM with an explicit pair"
        )
    if scale is not None and scale <= 0:
        raise ValueError(f"strike scale must be positive, got {scale!r}")
    return ListedUnderlying(
        code=base.code,
        name=base.name,
        pair=(pair if pair not in (None, "") else base.pair),
        invert=base.invert if invert is None else bool(invert),
        scale=base.scale if scale is None else float(scale),
        note=base.note,
    )


# ---------------------------------------------------------------------------
# the pasted table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Quote:
    """One listed strike and its market volatility, in decimals."""

    strike: float
    vol: float
    kind: str | None = None      # 'C', 'P' or None
    weight: float = 1.0
    line: int = 0


@dataclass(frozen=True)
class ParsedTable:
    quotes: tuple[Quote, ...]
    delimiter: str
    header: tuple[str, ...] | None
    strike_column: int
    vol_column: str
    vol_unit: str
    notes: tuple[str, ...] = ()
    skipped: tuple[tuple[int, str, str], ...] = ()


_HEAD_STRIKE = ("strike", "strikes", "k", "exercise", "exercise price", "px", "strike price")
_HEAD_VOL = ("vol", "iv", "impvol", "imp vol", "implied", "implied vol", "implied volatility",
             "sigma", "mid", "midvol", "mid vol", "settlement vol", "settle vol", "ivol")
_HEAD_BID = ("bid", "bidvol", "bid vol", "iv bid", "bid iv")
_HEAD_ASK = ("ask", "offer", "askvol", "ask vol", "iv ask", "ask iv", "offer vol")
_HEAD_KIND = ("type", "cp", "c/p", "callput", "call/put", "put/call", "option type", "right")
_HEAD_WEIGHT = ("weight", "wgt", "w")

_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")


def _clean(cell: str) -> str:
    return cell.strip().strip('"').strip("'")


def _to_float(cell: str) -> float | None:
    s = _clean(cell).replace(",", "").replace("%", "").replace("_", "")
    if s in ("", "-", "--", "n/a", "N/A", "na", "NA", "#N/A"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    if not _NUM.match(s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _split(line: str, delim: str) -> list[str]:
    if delim == "ws":
        return line.split()
    return line.split(delim)


def _detect_delimiter(lines: list[str]) -> str:
    """Tabs beat semicolons beat commas beat whitespace.

    Commas are checked last among the punctuation because ``1,425.00`` is a
    strike, not two fields; a table that really is comma separated will have
    the same comma count on every row, which a thousands separator will not
    reliably produce.
    """
    if any("\t" in ln for ln in lines):
        return "\t"
    if any(";" in ln for ln in lines):
        return ";"
    counts = {ln.count(",") for ln in lines}
    if counts and 0 not in counts and len(counts) == 1:
        return ","
    return "ws"


def parse_quote_table(text: str, *, vol_unit: str = "auto",
                      strike_column: int | None = None,
                      vol_column: int | None = None) -> ParsedTable:
    """Read a pasted strike/volatility table.

    ``strike_column`` and ``vol_column`` are 1-based and override the
    inference entirely; leave them ``None`` to let the headers decide.  Every
    inference is recorded in ``notes`` and every unusable line in ``skipped``,
    because a silently dropped strike is a silently wrong smile.
    """
    raw = [ln for ln in (text or "").splitlines()]
    lines = [(i + 1, ln) for i, ln in enumerate(raw) if ln.strip()]
    if not lines:
        raise ValueError("no rows to parse: the pasted table is empty")

    delim = _detect_delimiter([ln for _, ln in lines])
    notes: list[str] = []
    skipped: list[tuple[int, str, str]] = []

    rows = [(n, [_clean(c) for c in _split(ln, delim)]) for n, ln in lines]

    # -- header ----------------------------------------------------------
    header: tuple[str, ...] | None = None
    first = rows[0][1]
    if first and not any(_to_float(c) is not None for c in first):
        header = tuple(h.lower() for h in first)
        rows = rows[1:]
        if not rows:
            raise ValueError("the table has a header row but no data rows")

    def find(names) -> int | None:
        if header is None:
            return None
        for i, h in enumerate(header):
            if h in names:
                return i
        for i, h in enumerate(header):          # loosen: substring match
            if any(h.startswith(n) for n in names):
                return i
        return None

    i_strike = find(_HEAD_STRIKE)
    i_vol = find(_HEAD_VOL)
    i_bid, i_ask = find(_HEAD_BID), find(_HEAD_ASK)
    i_kind = find(_HEAD_KIND)
    i_weight = find(_HEAD_WEIGHT)

    if strike_column is not None:
        i_strike = int(strike_column) - 1
        notes.append(f"strike taken from column {strike_column} as instructed")
    if vol_column is not None:
        i_vol, i_bid, i_ask = int(vol_column) - 1, None, None
        notes.append(f"volatility taken from column {vol_column} as instructed")

    if i_strike is None:
        i_strike = 0
        notes.append("no strike header found; took the first column as the strike")
    if i_vol is None and i_bid is not None and i_ask is not None:
        notes.append(f"volatility is the mid of the '{header[i_bid]}' and '{header[i_ask]}' columns")
    elif i_vol is None:
        i_vol = i_strike + 1
        notes.append(f"no volatility header found; took column {i_vol + 1} as the volatility")

    vol_label = (header[i_vol] if header and i_vol is not None and i_vol < len(header)
                 else (f"mid of columns {i_bid + 1}/{i_ask + 1}" if i_vol is None
                       else f"column {i_vol + 1}"))

    # -- values ----------------------------------------------------------
    staged: list[tuple[int, float, float, str | None, float]] = []
    for n, cells in rows:
        def cell(idx):
            return cells[idx] if idx is not None and 0 <= idx < len(cells) else ""

        k = _to_float(cell(i_strike))
        if k is None:
            skipped.append((n, delim.join(cells) if delim != "ws" else " ".join(cells),
                            f"column {i_strike + 1} is not a number"))
            continue
        if i_vol is None:
            b, a = _to_float(cell(i_bid)), _to_float(cell(i_ask))
            v = None if b is None or a is None else 0.5 * (b + a)
            why = "bid or ask is not a number"
        else:
            v = _to_float(cell(i_vol))
            why = f"column {i_vol + 1} is not a number"
        if v is None:
            skipped.append((n, delim.join(cells) if delim != "ws" else " ".join(cells), why))
            continue
        if k <= 0:
            skipped.append((n, str(k), "strike is not positive"))
            continue
        if v <= 0:
            skipped.append((n, str(v), "volatility is not positive"))
            continue

        kind = None
        kt = _clean(cell(i_kind)).upper() if i_kind is not None else ""
        if kt[:1] in ("C", "P"):
            kind = kt[:1]
        w = _to_float(cell(i_weight)) if i_weight is not None else None
        staged.append((n, k, v, kind, 1.0 if w is None or w <= 0 else w))

    if not staged:
        detail = "; ".join(f"line {n}: {why}" for n, _, why in skipped[:4])
        raise ValueError(
            f"no usable strike/volatility rows found in the pasted table"
            + (f" ({detail})" if detail else "")
        )

    # -- units -----------------------------------------------------------
    vals = [v for _, _, v, _, _ in staged]
    unit = vol_unit.lower()
    if unit == "auto":
        if all(v > 1.0 for v in vals):
            unit = "percent"
        elif all(v < 1.0 for v in vals):
            unit = "decimal"
        else:
            straddling = sorted({round(v, 6) for v in vals if 0.9 < v < 1.6})
            raise ValueError(
                f"cannot tell whether these volatilities are percent or decimals: they "
                f"straddle 1.0 ({', '.join(str(v) for v in straddling[:6])}). "
                f"Set the volatility unit explicitly."
            )
        notes.append(f"volatilities read as {unit}")
    if unit not in ("percent", "decimal"):
        raise ValueError(f"unknown volatility unit {vol_unit!r}; expected 'auto', 'percent' or 'decimal'")
    div = 100.0 if unit == "percent" else 1.0

    quotes = []
    for n, k, v, kind, w in staged:
        vv = v / div
        if vv > 5.0:
            skipped.append((n, str(v), f"volatility of {vv:.1%} is beyond anything the "
                                       f"lognormal formulae can represent"))
            continue
        quotes.append(Quote(strike=k, vol=vv, kind=kind, weight=w, line=n))
    if not quotes:
        raise ValueError("every row was rejected once the volatility unit was applied")

    quotes.sort(key=lambda q: q.strike)
    return ParsedTable(
        quotes=tuple(quotes), delimiter={"\t": "tab", ";": "semicolon", ",": "comma",
                                         "ws": "whitespace"}[delim],
        header=header, strike_column=(i_strike + 1), vol_column=vol_label, vol_unit=unit,
        notes=tuple(notes), skipped=tuple(skipped),
    )


def dedupe(quotes, forward: float) -> tuple[tuple[Quote, ...], tuple[str, ...]]:
    """Collapse repeated strikes, keeping the out-of-the-money side.

    An exchange lists a call and a put at every strike.  Their settlement
    volatilities are rarely identical, and the in-the-money one is the less
    reliable of the two -- it has little time value, so a one-tick price
    difference moves its implied volatility a long way.  Where both are
    present the out-of-the-money quote wins; where the type is unknown the two
    are averaged and the fact is reported.
    """
    by_strike: dict[float, list[Quote]] = {}
    for q in quotes:
        by_strike.setdefault(round(q.strike, 12), []).append(q)
    out, notes = [], []
    for k in sorted(by_strike):
        group = by_strike[k]
        if len(group) == 1:
            out.append(group[0])
            continue
        want = "C" if k >= forward else "P"
        otm = [q for q in group if q.kind == want]
        if otm:
            out.append(otm[0])
            notes.append(f"strike {k:g}: kept the out-of-the-money {want} quote of "
                         f"{len(group)} rows")
        else:
            mean = float(np.mean([q.vol for q in group]))
            out.append(Quote(strike=k, vol=mean, kind=None,
                             weight=float(np.mean([q.weight for q in group])),
                             line=group[0].line))
            notes.append(f"strike {k:g}: averaged {len(group)} quotes spanning "
                         f"{min(q.vol for q in group):.4%}–{max(q.vol for q in group):.4%}")
    return tuple(out), tuple(notes)


# ---------------------------------------------------------------------------
# the fit
# ---------------------------------------------------------------------------

WEIGHTINGS = ("vega", "equal", "table")


@dataclass(frozen=True)
class QuoteFit:
    """A SABR curve fitted to a listed table, with the evidence for it."""

    params: SabrParams
    beta: float
    forward: float
    t: float
    strikes: tuple[float, ...]
    market_vols: tuple[float, ...]
    model_vols: tuple[float, ...]
    weights: tuple[float, ...]
    rmse: float                     # weighted, in decimals
    max_error: float                # signed model - market, largest in absolute value
    max_error_strike: float
    converged: bool
    message: str
    weighting: str = "vega"
    warnings: tuple[str, ...] = ()

    @property
    def residuals(self) -> tuple[float, ...]:
        return tuple(m - q for m, q in zip(self.model_vols, self.market_vols))

    @property
    def degrees_of_freedom(self) -> int:
        return len(self.strikes) - 3

    def vol(self, strike):
        """The fitted volatility at any strike, in the listed contract's units."""
        return lognormal_vol(strike, self.params)


def _weights_for(strikes, vols, forward: float, t: float, weighting: str,
                 table_weights) -> np.ndarray:
    """Turn a weighting choice into a normalised weight vector."""
    ks = np.asarray(strikes, dtype=float)
    vs = np.asarray(vols, dtype=float)
    if weighting == "equal":
        w = np.ones_like(ks)
    elif weighting == "table":
        w = np.asarray(table_weights, dtype=float)
        if np.any(w <= 0) or not np.all(np.isfinite(w)):
            raise ValueError("table weights must all be positive and finite")
    elif weighting == "vega":
        # Vega weighting says a quarter of a vol point at the money matters
        # more than a quarter of a vol point in a 5-delta wing, which is what
        # a book actually experiences.  It also stops a handful of illiquid
        # far strikes dragging the whole curve.
        w = np.asarray(black.vega(forward, ks, vs, t), dtype=float)
        if not np.all(np.isfinite(w)) or np.all(w <= 0):
            raise ValueError(
                f"vega weighting produced no usable weights at t={t:.4f}y with forward "
                f"{forward:g}; the strikes may be far outside anything this expiry can reach"
            )
        w = np.maximum(w, np.max(w) * 1e-6)
    else:
        raise ValueError(f"unknown weighting {weighting!r}; expected one of {WEIGHTINGS}")
    return w / float(np.mean(w))


def _alpha_bracket(rho: float, nu: float, t: float, f: float, beta: float,
                   lo_vol: float, hi_vol: float) -> tuple[float, float] | None:
    """A bracket for alpha, from the closed-form at-the-forward condition.

    Hagan's at-the-money expansion inverts exactly for alpha, so the alphas
    that would put the *whole curve* at the lowest and highest quoted
    volatilities bound the alpha that fits them jointly.  Widening by a factor
    of four each way covers the fact that none of the quotes need be at the
    forward.
    """
    lo = alpha_roots_at_forward(max(lo_vol, 1e-6), rho, nu, t, f, beta)
    hi = alpha_roots_at_forward(hi_vol, rho, nu, t, f, beta)
    if not lo or not hi:
        return None
    a, b = lo[0] / 4.0, hi[0] * 4.0
    if not (math.isfinite(a) and math.isfinite(b)) or b <= a:
        return None
    return max(a, 1e-9), min(b, 100.0)


def fit_sabr(strikes, vols, t: float, forward: float, *, beta: float = 1.0,
             weighting: str = "vega", table_weights=None,
             scan: tuple[int, int] = (15, 11), rho_bound: float = 0.999) -> QuoteFit:
    """Least-squares SABR through N listed strike/volatility points.

    The three-quote calibration in :mod:`volkit.sabr` eliminates alpha with the
    at-the-money condition and is left with a two-dimensional problem.  Here
    there is no at-the-money quote to eliminate it with, so alpha is instead
    *profiled out*: at every ``(rho, nu)`` the alpha minimising the weighted
    sum of squares is found by a bounded scalar search inside a bracket that
    comes from the closed-form at-the-forward inversion.  The outer problem
    stays two-dimensional and is swept over its whole admissible box before
    anything is polished, so the answer does not depend on a starting guess --
    the same reasoning as the three-quote fit, for the same reason.

    ``nu`` is carried through the sweep as the scale-free ``nu * sqrt(t)``
    because that is the quantity whose sensible range does not depend on the
    expiry.
    """
    ks = np.asarray(strikes, dtype=float)
    vs = np.asarray(vols, dtype=float)
    if ks.ndim != 1 or ks.shape != vs.shape:
        raise ValueError("strikes and volatilities must be one-dimensional and the same length")
    if t <= 0:
        raise ValueError(f"time to expiry must be positive, got {t!r}")
    if forward <= 0:
        raise ValueError(f"forward must be positive, got {forward!r}")
    if not 0.0 < beta <= 1.0:
        raise ValueError(f"beta must lie in (0, 1], got {beta!r}")
    if np.any(ks <= 0):
        raise ValueError("every strike must be positive")
    if np.any(vs <= 0):
        raise ValueError("every volatility must be positive")
    n = ks.size
    if n < 3:
        raise ValueError(
            f"SABR has three free parameters at fixed beta and only {n} "
            f"quote{'s' if n != 1 else ''} were given; at least 3 distinct strikes are needed"
        )
    if np.unique(np.round(ks, 12)).size < 3:
        raise ValueError("at least 3 *distinct* strikes are needed; the table repeats strikes")

    w = _weights_for(ks, vs, forward, t, weighting, table_weights)
    sqt = math.sqrt(t)
    lo_vol, hi_vol = float(np.min(vs)), float(np.max(vs))

    def model(alpha: float, rho: float, nu: float) -> np.ndarray:
        return np.asarray(
            lognormal_vol(ks, SabrParams(alpha, rho, nu, t, beta, forward)), dtype=float)

    def wsse(alpha: float, rho: float, nu: float) -> float:
        try:
            m = model(alpha, rho, nu)
        except (ValueError, ArithmeticError):
            return 1e6
        if not np.all(np.isfinite(m)):
            return 1e6
        return float(np.sum((w * (m - vs)) ** 2))

    def profile_alpha(rho: float, nu: float) -> tuple[float, float] | None:
        """The best alpha at this (rho, nu), and the cost there."""
        br = _alpha_bracket(rho, nu, t, forward, beta, lo_vol, hi_vol)
        if br is None:
            return None
        res = minimize_scalar(lambda a: wsse(a, rho, nu), bounds=br, method="bounded",
                              options={"xatol": 1e-12, "maxiter": 200})
        if not res.success or not math.isfinite(res.fun):
            return None
        return float(res.x), float(res.fun)

    # -- sweep the admissible box ----------------------------------------
    n_rho, n_nu = scan
    best: tuple[float, float, float, float] | None = None      # cost, alpha, rho, nu
    nodes: list[tuple[float, float, float, float]] = []
    for rho in np.linspace(-0.95, 0.95, n_rho):
        for s in np.geomspace(0.05, 3.0, n_nu):
            nu = float(s) / sqt
            got = profile_alpha(float(rho), nu)
            if got is None:
                continue
            alpha, cost = got
            nodes.append((cost, alpha, float(rho), nu))
    if not nodes:
        raise ConvergenceError(
            f"no admissible SABR parameters exist for these quotes at t={t:.4f}y "
            f"(forward {forward:g}, volatilities {lo_vol:.4%}–{hi_vol:.4%}); "
            f"Hagan's at-the-money condition has no positive alpha anywhere on the sweep"
        )
    nodes.sort(key=lambda z: z[0])
    best = nodes[0]

    # -- polish over all three parameters --------------------------------
    # Alpha is carried in logs so the optimiser cannot step it negative, and
    # nu as nu*sqrt(t) so all three variables are O(1).
    def residuals(x: np.ndarray) -> np.ndarray:
        alpha, rho, s = math.exp(float(x[0])), float(x[1]), float(x[2])
        try:
            m = model(alpha, rho, s / sqt)
        except (ValueError, ArithmeticError):
            return np.full(n, 1e3)
        if not np.all(np.isfinite(m)):
            return np.full(n, 1e3)
        return w * (m - vs)

    x0 = np.array([math.log(max(best[1], 1e-9)), best[2], best[3] * sqt])
    lo = np.array([math.log(1e-9), -rho_bound, 1e-4])
    hi = np.array([math.log(100.0), rho_bound, 5.0])
    try:
        sol = least_squares(residuals, np.clip(x0, lo + 1e-12, hi - 1e-12), bounds=(lo, hi),
                            xtol=1e-14, ftol=1e-14, gtol=1e-14, max_nfev=1200)
        alpha, rho, nu = math.exp(float(sol.x[0])), float(sol.x[1]), float(sol.x[2]) / sqt
        ok = bool(sol.success)
        why = "converged" if ok else f"least-squares stopped: {sol.message}"
    except Exception as exc:  # noqa: BLE001 - fall back to the sweep node, but say so
        alpha, rho, nu = best[1], best[2], best[3]
        ok, why = False, f"polish failed ({type(exc).__name__}: {exc}); reporting the sweep node"

    params = SabrParams(alpha, rho, nu, t, beta, forward)
    m = model(alpha, rho, nu)
    err = m - vs
    j = int(np.argmax(np.abs(err)))
    rmse = float(math.sqrt(np.mean((w * err) ** 2)))

    warnings: list[str] = []
    if n == 3:
        warnings.append(
            "three quotes and three parameters: this is an exact interpolation, not a fit. "
            "The residuals will be zero whatever the quotes say, so they are no evidence "
            "the shape is right."
        )
    if rmse > 0.0025:
        warnings.append(
            f"weighted RMSE is {rmse * 100:.3f} vol points; the largest miss is "
            f"{err[j] * 100:+.3f} at strike {ks[j]:g}. A SABR shape cannot pass through "
            f"these quotes - check for a mixed expiry, a stale strike, or a units error."
        )
    if float(np.min(ks)) > forward or float(np.max(ks)) < forward:
        warnings.append(
            f"the quoted strikes {float(np.min(ks)):g}–{float(np.max(ks)):g} do not bracket "
            f"the forward {forward:g}; only one wing is being fitted and the level is an "
            f"extrapolation"
        )
    if abs(rho) > rho_bound - 1e-3:
        warnings.append(
            f"rho hit its bound at {rho:+.4f}: the quotes want a steeper skew than SABR "
            f"can produce at beta={beta:g}"
        )
    warnings.extend(arbitrage_warnings(params, float(np.min(ks)), float(np.max(ks))))

    return QuoteFit(
        params=params, beta=beta, forward=forward, t=t,
        strikes=tuple(float(k) for k in ks),
        market_vols=tuple(float(v) for v in vs),
        model_vols=tuple(float(v) for v in m),
        weights=tuple(float(x) for x in w),
        rmse=rmse, max_error=float(err[j]), max_error_strike=float(ks[j]),
        converged=ok, message=why, weighting=weighting, warnings=tuple(warnings),
    )


def arbitrage_warnings(p: SabrParams, k_lo: float, k_hi: float, n: int = 241) -> list[str]:
    """Flag strike ranges where the fitted curve implies a negative density.

    Hagan's expansion is an asymptotic formula, not a model, and it is well
    known to produce butterfly arbitrage in the wings at longer expiries.  The
    project's rule is that a real risk is marked rather than assumed away, so
    this reports the arbitrage instead of clipping the curve to hide it.
    """
    lo, hi = k_lo * 0.85, k_hi * 1.15
    ks = np.linspace(lo, hi, n)
    try:
        vols = np.asarray(lognormal_vol(ks, p), dtype=float)
        pv = np.asarray(black.price(p.f, ks, vols, p.t, True), dtype=float) / p.f
    except (ValueError, ArithmeticError):
        return [f"could not check the fitted curve for arbitrage between {lo:g} and {hi:g}"]
    if not np.all(np.isfinite(pv)):
        return [f"the fitted curve is not finite everywhere between {lo:g} and {hi:g}"]
    # Everything is done per unit of forward, in moneyness rather than in the
    # contract's own strike units.  Black-76 is homogeneous, so this changes no
    # answer -- but it makes the density O(1) whether the contract trades at
    # 0.0068 or at 4200, and a fixed tolerance then means the same thing for
    # both.  Compared in raw units, the second difference of a yen future's
    # prices is small enough that rounding alone can look like arbitrage.
    h = (ks[1] - ks[0]) / p.f
    dens = (pv[:-2] - 2.0 * pv[1:-1] + pv[2:]) / (h * h)
    bad = np.where(dens < -1e-9)[0]
    if bad.size == 0:
        return []
    a, b = float(ks[bad[0] + 1]), float(ks[bad[-1] + 1])
    return [
        f"the fitted SABR curve implies a negative risk-neutral density between strikes "
        f"{a:g} and {b:g} ({bad.size} of {n - 2} sample points). Hagan's expansion is "
        f"asymptotic and does this in the wings; do not price strikes in that range off "
        f"this curve."
    ]


# ---------------------------------------------------------------------------
# comparison against the marked FX surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Anchor:
    """A point on the marked smile, restated on the listed axis."""

    label: str
    fx_strike_ratio: float
    fx_strike: float
    listed_strike: float
    book_vol: float
    fit_vol: float

    @property
    def diff(self) -> float:
        return self.fit_vol - self.book_vol


@dataclass(frozen=True)
class SurfaceComparison:
    pair: str
    cut: str
    method: str
    forward_fx: float
    book_atm: float
    book_vols: tuple[float, ...]          # at the quoted listed strikes
    anchors: tuple[Anchor, ...]
    warnings: tuple[str, ...] = ()

    @property
    def rr_book(self) -> dict[str, float]:
        return _rr_from_anchors(self.anchors, "book_vol")

    @property
    def rr_listed(self) -> dict[str, float]:
        return _rr_from_anchors(self.anchors, "fit_vol")


def _rr_from_anchors(anchors, attr: str) -> dict[str, float]:
    by = {a.label: getattr(a, attr) for a in anchors}
    out: dict[str, float] = {}
    for d in (25, 10):
        c, p = by.get(f"{d}d call"), by.get(f"{d}d put")
        if c is not None and p is not None:
            out[f"rr{d}"] = c - p
            atm = by.get("ATM")
            if atm is not None:
                out[f"fly{d}"] = 0.5 * (c + p) - atm
    return out


def compare_to_surface(fit: QuoteFit, surface, expiry, u: ListedUnderlying, *,
                       method: str | None = None, cut: str = "NY",
                       deltas=(0.10, 0.25)) -> SurfaceComparison:
    """Put the fitted listed curve and the marked OTC surface on one axis.

    Everything here is done at **matched physical strikes**.  It would be
    tempting to compare the listed fit's 25-delta risk reversal against the
    book's, but a listed contract on an inverted underlying measures delta in
    the other currency, and the two 25-delta strikes are then not the same
    strike.  Comparing volatilities strike by strike sidesteps the whole
    question: whatever convention either side quotes in, a 145.00 USDJPY
    option is a 145.00 USDJPY option.

    The risk reversals reported alongside are therefore *the book's delta
    strikes, read off both curves* -- a slope comparison at fixed strikes, not
    a quote comparison, and labelled that way.
    """
    if u.pair is None:
        raise ValueError("this underlying is not mapped to a currency pair; nothing to compare")
    fx_forward = u.to_fx(fit.forward)
    warnings: list[str] = []
    if fit.beta != 1.0:
        warnings.append(
            f"the listed curve was fitted at beta={fit.beta:g} but the book's smile is "
            f"lognormal (beta=1); the two are different models, not two marks of one"
        )

    fx_ks = np.asarray(u.to_fx(np.asarray(fit.strikes, dtype=float)), dtype=float)
    ratios = fx_ks / fx_forward
    book_vols = tuple(float(v) for v in np.atleast_1d(
        np.asarray(surface.vol(ratios, expiry, method, cut), dtype=float)))
    warnings.extend(surface.band_check(fx_ks, fx_forward))

    table = surface.smile_table(expiry, deltas=tuple(deltas), method=method, cut=cut)
    anchors = []
    for row in table:
        fx_k = float(row["strike"]) * fx_forward
        listed_k = float(u.from_fx(fx_k))
        anchors.append(Anchor(
            label=row["label"], fx_strike_ratio=float(row["strike"]), fx_strike=fx_k,
            listed_strike=listed_k, book_vol=float(row["vol"]),
            fit_vol=float(lognormal_vol(listed_k, fit.params)),
        ))

    lo, hi = min(fit.strikes), max(fit.strikes)
    outside = [a.label for a in anchors if not lo <= a.listed_strike <= hi]
    if outside:
        warnings.append(
            f"the book's {', '.join(outside)} strike{'s' if len(outside) > 1 else ''} "
            f"lies outside the quoted range {lo:g}–{hi:g}; the listed curve is being "
            f"extrapolated there"
        )
    return SurfaceComparison(
        pair=u.pair, cut=cut, method=method or getattr(surface, "method", "SVI"),
        forward_fx=fx_forward, book_atm=float(surface.atm_vol(expiry, cut)),
        book_vols=book_vols, anchors=tuple(anchors), warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# one panel, end to end
# ---------------------------------------------------------------------------


def _normalise_expiry(value):
    """Accept what a browser's ``datetime-local`` input produces.

    ``timeutil.parse_datetime`` takes a space between the date and the time;
    an HTML date/time field emits a ``T``, and a listed expiry always carries
    a time of day because the exchange settles at a fixed hour.  Trailing
    seconds and a ``Z`` are tolerated for the same reason.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        raise ValueError("the panel has no expiry")
    if text.endswith("Z"):
        text = text[:-1]
    if "+" in text[10:]:
        text = text[:10] + text[10:].split("+")[0]
    return text.replace("T", " ").strip()


@dataclass
class Panel:
    """One expiry/underlying combination: the unit the UI creates and destroys."""

    underlying: ListedUnderlying
    expiry: object                      # date, datetime or tenor string
    forward: float
    quotes: tuple[Quote, ...]
    beta: float = 1.0
    weighting: str = "vega"
    method: str | None = None
    cut: str = "NY"
    label: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    def run(self, book=None, *, clock=None, curve_points: int = 161) -> dict:
        """Fit, compare, and return everything the screen or the CLI needs.

        The result is plain JSON-safe data: the panel itself holds no state
        between calls, so the browser can own the list of panels and the
        server stays a pure function of what it is sent.
        """
        surface = None
        if book is not None and self.underlying.pair:
            if self.underlying.pair not in book:
                raise ValueError(
                    f"{self.underlying.pair} is not in the book; the panel's underlying "
                    f"{self.underlying.code} maps to it. Add the pair or choose CUSTOM."
                )
            surface = book[self.underlying.pair]
        the_clock = clock or (surface.clock if surface is not None else None)
        if the_clock is None:
            raise ValueError("a clock is required to price a listed expiry; pass one, or a book")

        expiry_dt = the_clock.coerce_datetime(_normalise_expiry(self.expiry))
        t = the_clock.years_to(expiry_dt)
        if t <= 0:
            raise ValueError(
                f"expiry {expiry_dt:%Y-%m-%d %H:%M}Z is not in the future "
                f"(valuation {the_clock.now:%Y-%m-%d %H:%M}Z)"
            )

        quotes, dedupe_notes = dedupe(self.quotes, self.forward)
        fit = fit_sabr([q.strike for q in quotes], [q.vol for q in quotes], t, self.forward,
                       beta=self.beta, weighting=self.weighting,
                       table_weights=[q.weight for q in quotes])

        cmp_ = None
        if surface is not None:
            cmp_ = compare_to_surface(fit, surface, expiry_dt, self.underlying,
                                      method=self.method, cut=self.cut)

        rows = []
        for i, q in enumerate(quotes):
            row = {
                "strike": q.strike,
                "kind": q.kind,
                "market_vol": q.vol * 100.0,
                "fit_vol": fit.model_vols[i] * 100.0,
                "fit_diff": (fit.model_vols[i] - q.vol) * 100.0,
                "weight": fit.weights[i],
                "moneyness": q.strike / self.forward,
                "book_vol": None,
                "book_diff": None,
                "fx_strike": None,
            }
            if cmp_ is not None:
                row["book_vol"] = cmp_.book_vols[i] * 100.0
                row["book_diff"] = (fit.model_vols[i] - cmp_.book_vols[i]) * 100.0
                row["fx_strike"] = float(self.underlying.to_fx(q.strike))
            rows.append(row)

        lo, hi = min(fit.strikes) * 0.94, max(fit.strikes) * 1.06
        grid = np.linspace(lo, hi, curve_points)
        curve = {
            "strikes": [float(k) for k in grid],
            "fit": [float(v) * 100.0 for v in np.asarray(lognormal_vol(grid, fit.params))],
            "book": None,
        }
        if cmp_ is not None:
            fx_grid = np.asarray(self.underlying.to_fx(grid), dtype=float)
            curve["book"] = [float(v) * 100.0 for v in np.atleast_1d(
                np.asarray(surface.vol(fx_grid / cmp_.forward_fx, expiry_dt,
                                       self.method, self.cut), dtype=float))]

        atm_fit = float(lognormal_vol(self.forward, fit.params))
        out = {
            "label": self.label,
            "underlying": {"code": self.underlying.code, "name": self.underlying.name,
                           "pair": self.underlying.pair, "invert": self.underlying.invert,
                           "scale": self.underlying.scale, "note": self.underlying.note},
            "expiry": expiry_dt.isoformat(),
            "valuation": the_clock.now.isoformat(),
            "years": t,
            "days": t * 365.2425,
            "forward": self.forward,
            "n_quotes": len(quotes),
            "fit": {
                "alpha": fit.params.alpha, "rho": fit.params.rho, "volvol": fit.params.volvol,
                "log_volvol": fit.params.log_volvol, "beta": fit.beta,
                "atm_vol": atm_fit * 100.0,
                "rmse": fit.rmse * 100.0, "max_error": fit.max_error * 100.0,
                "max_error_strike": fit.max_error_strike,
                "converged": fit.converged, "message": fit.message,
                "weighting": fit.weighting, "dof": fit.degrees_of_freedom,
            },
            "rows": rows,
            "curve": curve,
            "notes": list(self.notes) + list(dedupe_notes),
            "warnings": list(fit.warnings),
            "comparison": None,
        }
        if cmp_ is not None:
            out["warnings"].extend(cmp_.warnings)
            out["comparison"] = {
                "pair": cmp_.pair, "cut": cmp_.cut, "method": cmp_.method,
                "forward_fx": cmp_.forward_fx,
                "book_atm": cmp_.book_atm * 100.0,
                "atm_diff": (atm_fit - cmp_.book_atm) * 100.0,
                "anchors": [{"label": a.label, "listed_strike": a.listed_strike,
                             "fx_strike": a.fx_strike, "book_vol": a.book_vol * 100.0,
                             "fit_vol": a.fit_vol * 100.0, "diff": a.diff * 100.0}
                            for a in cmp_.anchors],
                "rr_book": {k: v * 100.0 for k, v in cmp_.rr_book.items()},
                "rr_listed": {k: v * 100.0 for k, v in cmp_.rr_listed.items()},
            }
        return out


def panel_from_request(payload: dict) -> Panel:
    """Build a Panel from a JSON body or a CLI namespace-like mapping."""
    u = resolve_underlying(
        payload.get("underlying") or payload.get("code"),
        pair=payload.get("pair"),
        invert=(None if payload.get("invert") in (None, "") else
                str(payload["invert"]).lower() in ("1", "true", "yes", "on")),
        scale=(None if payload.get("scale") in (None, "") else float(payload["scale"])),
    )
    if "quotes" in payload and payload["quotes"]:
        quotes = tuple(Quote(strike=float(q["strike"]), vol=float(q["vol"]),
                             kind=q.get("kind"), weight=float(q.get("weight", 1.0) or 1.0))
                       for q in payload["quotes"])
        notes: tuple[str, ...] = ()
    else:
        parsed = parse_quote_table(
            payload.get("text", ""),
            vol_unit=payload.get("vol_unit", "auto") or "auto",
            strike_column=(int(payload["strike_column"])
                           if payload.get("strike_column") not in (None, "") else None),
            vol_column=(int(payload["vol_column"])
                        if payload.get("vol_column") not in (None, "") else None),
        )
        quotes = parsed.quotes
        notes = (f"{len(parsed.quotes)} rows read, {parsed.delimiter} separated, "
                 f"strike from column {parsed.strike_column}, volatility from "
                 f"{parsed.vol_column}",) + parsed.notes
        notes += tuple(f"line {n} skipped ({why}): {txt[:60]}" for n, txt, why in parsed.skipped)

    forward = payload.get("forward")
    if forward in (None, ""):
        raise ValueError("a forward (the futures price) is required to fit a listed curve")
    forward = float(forward)

    return Panel(
        underlying=u,
        expiry=payload.get("expiry"),
        forward=forward,
        quotes=quotes,
        beta=float(payload.get("beta") or 1.0),
        weighting=(payload.get("weighting") or "vega"),
        method=(payload.get("method") or None),
        cut=(payload.get("cut") or "NY"),
        label=(payload.get("label") or ""),
        notes=notes,
    )
