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
from dataclasses import dataclass, field, replace

import numpy as np
from scipy.optimize import least_squares, minimize_scalar

from . import black
from .numerics import ConvergenceError
from .sabr import SabrParams, alpha_roots_at_forward, atm_vol, lognormal_vol

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

    ``contract_size`` is how many units of the contract's base currency one
    option covers -- 125,000 euros for ``6E``, 12,500,000 yen for ``6J``.  It
    is what turns a per-unit greek into money, and nothing else in this module
    uses it; a fit needs no notion of size.  ``CUSTOM`` has none, and a
    positions panel on a custom contract says so rather than quietly working
    in units of one.
    """

    code: str
    name: str
    pair: str | None = None
    invert: bool = False
    scale: float = 1.0
    note: str = ""
    contract_size: float = 0.0

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


def _u(code, name, pair=None, invert=False, scale=1.0, note="", size=0.0):
    return ListedUnderlying(code, name, pair, invert, scale, note, size)


# The CME quotes every currency future as US dollars per unit of the foreign
# currency, which is the market convention for four of these pairs and the
# reciprocal of it for the other five.  ``invert`` records which.
UNDERLYINGS: dict[str, ListedUnderlying] = {
    u.code: u for u in (
        _u("CUSTOM", "custom / specify the pair yourself"),
        _u("6E", "CME euro future", "EURUSD", False, size=125_000),
        _u("6B", "CME sterling future", "GBPUSD", False, size=62_500),
        _u("6A", "CME Australian dollar future", "AUDUSD", False, size=100_000),
        _u("6N", "CME New Zealand dollar future", "NZDUSD", False, size=100_000),
        _u("6J", "CME yen future", "USDJPY", True, size=12_500_000,
           note="quoted in USD per JPY; strikes listed as integers need scale 1e-6"),
        _u("6C", "CME Canadian dollar future", "USDCAD", True, size=100_000),
        _u("6S", "CME Swiss franc future", "USDCHF", True, size=125_000),
        _u("6M", "CME Mexican peso future", "USDMXN", True, size=500_000),
        _u("6L", "CME Brazilian real future", "USDBRL", True, size=100_000),
        _u("6Z", "CME South African rand future", "USDZAR", True, size=500_000),
        _u("E7", "CME E-mini euro future", "EURUSD", False, size=62_500),
        _u("J7", "CME E-mini yen future", "USDJPY", True, size=6_250_000),
    )
}


def resolve_underlying(code: str | None, *, pair: str | None = None,
                       invert: bool | None = None, scale: float | None = None,
                       contract_size: float | None = None) -> ListedUnderlying:
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
    if contract_size is not None and contract_size <= 0:
        raise ValueError(f"contract size must be positive, got {contract_size!r}")
    return ListedUnderlying(
        code=base.code,
        name=base.name,
        pair=(pair if pair not in (None, "") else base.pair),
        invert=base.invert if invert is None else bool(invert),
        scale=base.scale if scale is None else float(scale),
        note=base.note,
        contract_size=(base.contract_size if contract_size is None else float(contract_size)),
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

# The three parameters the fit moves at fixed beta.  Any of them may instead
# be given -- a desk that knows the wing it wants, or a curve carried over
# from yesterday and nudged -- and the rest are fitted around it.
FIT_PARAMS = ("alpha", "rho", "volvol")


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
    # Which of alpha, rho and volvol were given rather than fitted, in the
    # order they are declared.  Reported because a residual means something
    # different when a parameter was held: the curve is not the best SABR
    # through these quotes, it is the best one through them *at* that value.
    fixed: tuple[str, ...] = ()

    @property
    def residuals(self) -> tuple[float, ...]:
        return tuple(m - q for m, q in zip(self.model_vols, self.market_vols))

    @property
    def free(self) -> tuple[str, ...]:
        return tuple(k for k in FIT_PARAMS if k not in self.fixed)

    @property
    def degrees_of_freedom(self) -> int:
        return len(self.strikes) - len(self.free)

    def vol(self, strike):
        """The fitted volatility at any strike, in the listed contract's units."""
        return lognormal_vol(strike, self.params)


# What a pinned parameter is called on the screen and in a message.  ``nu``
# rather than ``volvol`` because that is what the panel and the CLI print.
PIN_LABELS = {"alpha": "alpha", "rho": "rho", "volvol": "nu"}


def _pin_labels(held: dict) -> list[str]:
    return [f"{PIN_LABELS[k]}={held[k]:g}" for k in FIT_PARAMS if k in held]


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
             weighting: str = "vega", table_weights=None, fixed=None,
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

    ``fixed`` holds any of ``alpha``, ``rho`` and ``volvol`` that are to be
    *given* rather than fitted -- a wing a desk has a view on, or yesterday's
    curve carried over and nudged.  A pinned parameter is held everywhere:
    the sweep does not visit any other value of it and the polish does not
    hold it as a variable, so the answer is the best curve through the quotes
    **at** that value rather than the best curve overall.  Pin all three and
    nothing is fitted at all; the quotes are still priced against the curve
    and the residuals are reported, which is the point of doing it.
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

    # A pin given as blank or None is not a pin: the screen sends an empty
    # box for "let the fit decide", and reading that as a zero would mark a
    # curve nobody asked for.
    held: dict[str, float] = {}
    for key, raw in dict(fixed or {}).items():
        if key not in FIT_PARAMS:
            raise ValueError(f"unknown SABR parameter {key!r}; expected one of {FIT_PARAMS}")
        if raw in (None, ""):
            continue
        try:
            held[key] = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if "alpha" in held and not (held["alpha"] > 0 and math.isfinite(held["alpha"])):
        raise ValueError(f"alpha must be positive, got {held['alpha']!r}")
    if "rho" in held and not -1.0 < held["rho"] < 1.0:
        raise ValueError(f"rho must lie strictly between -1 and 1, got {held['rho']!r}")
    if "volvol" in held and not (held["volvol"] > 0 and math.isfinite(held["volvol"])):
        raise ValueError(f"nu must be positive, got {held['volvol']!r}")
    free = tuple(k for k in FIT_PARAMS if k not in held)

    n = ks.size
    if n < len(free):
        raise ValueError(
            f"SABR has {len(free)} free parameter{'s' if len(free) != 1 else ''} here "
            f"({', '.join(PIN_LABELS[k] for k in free) if free else 'none'}) and only {n} "
            f"quote{'s' if n != 1 else ''} were given; at least {len(free)} distinct "
            f"strikes are needed"
        )
    if n < 1:
        raise ValueError("no quotes were given, so there is nothing to fit or to price")
    if np.unique(np.round(ks, 12)).size < len(free):
        raise ValueError(
            f"at least {len(free)} *distinct* strikes are needed; the table repeats strikes")

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
        """The best alpha at this (rho, nu), and the cost there.

        A pinned alpha is not profiled -- it is the answer, and its cost is
        whatever it costs.
        """
        if "alpha" in held:
            cost = wsse(held["alpha"], rho, nu)
            return (held["alpha"], cost) if math.isfinite(cost) else None
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
    # A pinned direction is a sweep of one node, so the box being searched is
    # the slice through the pin rather than the whole of it.
    rho_grid = ([held["rho"]] if "rho" in held
                else list(np.linspace(-0.95, 0.95, n_rho)))
    nu_grid = ([held["volvol"] * sqt] if "volvol" in held
               else list(np.geomspace(0.05, 3.0, n_nu)))
    for rho in rho_grid:
        for s in nu_grid:
            nu = float(s) / sqt
            got = profile_alpha(float(rho), nu)
            if got is None:
                continue
            alpha, cost = got
            nodes.append((cost, alpha, float(rho), nu))
    if not nodes:
        raise ConvergenceError(
            f"no admissible SABR parameters exist for these quotes at t={t:.4f}y "
            f"(forward {forward:g}, volatilities {lo_vol:.4%}–{hi_vol:.4%}"
            + (f", with {', '.join(_pin_labels(held))} held" if held else "")
            + "); Hagan's at-the-money condition has no positive alpha anywhere on the sweep"
        )
    nodes.sort(key=lambda z: z[0])
    best = nodes[0]

    # -- polish the free parameters --------------------------------------
    # Alpha is carried in logs so the optimiser cannot step it negative, and
    # nu as nu*sqrt(t) so all three variables are O(1).  Only the free ones
    # are handed to the optimiser: a pinned parameter is substituted back at
    # every evaluation rather than bounded to a narrow interval, so it cannot
    # drift by so much as a rounding step from the number that was typed.
    slot = {"alpha": 0, "rho": 1, "volvol": 2}
    # The sweep only ever visited the pinned values, so this already carries
    # them; the free entries are the best node.
    base = np.array([math.log(max(best[1], 1e-9)), best[2], best[3] * sqt])
    lo_all = np.array([math.log(1e-9), -rho_bound, 1e-4])
    hi_all = np.array([math.log(100.0), rho_bound, 5.0])
    idx = [slot[k] for k in free]

    def expand(x) -> tuple[float, float, float]:
        """The three parameters from the free variables, pins substituted.

        A pin comes back from ``held`` and not out of ``base``: alpha travels
        through a logarithm there, and exp(log(a)) is not always a again.  A
        number somebody typed must come out as the number they typed.
        """
        full = base.copy()
        for j, i in enumerate(idx):
            full[i] = float(x[j])
        return (held["alpha"] if "alpha" in held else math.exp(float(full[0])),
                held["rho"] if "rho" in held else float(full[1]),
                held["volvol"] if "volvol" in held else float(full[2]) / sqt)

    def residuals(x: np.ndarray) -> np.ndarray:
        alpha, rho, nu = expand(x)
        try:
            m = model(alpha, rho, nu)
        except (ValueError, ArithmeticError):
            return np.full(n, 1e3)
        if not np.all(np.isfinite(m)):
            return np.full(n, 1e3)
        return w * (m - vs)

    if not idx:
        # Nothing to fit: every parameter was given.  That is a legitimate
        # thing to ask for -- price these quotes against this curve -- and it
        # says so rather than reporting a convergence it never attempted.
        alpha, rho, nu = expand([])
        ok, why = True, "no fit: alpha, rho and nu were all given"
    else:
        x0 = np.clip(base[idx], lo_all[idx] + 1e-12, hi_all[idx] - 1e-12)
        try:
            sol = least_squares(residuals, x0, bounds=(lo_all[idx], hi_all[idx]),
                                xtol=1e-14, ftol=1e-14, gtol=1e-14, max_nfev=1200)
            alpha, rho, nu = expand(sol.x)
            ok = bool(sol.success)
            why = "converged" if ok else f"least-squares stopped: {sol.message}"
            if held:
                why += f" ({', '.join(_pin_labels(held))} held)"
        except Exception as exc:  # noqa: BLE001 - fall back to the sweep node, but say so
            alpha, rho, nu = best[1], best[2], best[3]
            ok, why = False, (f"polish failed ({type(exc).__name__}: {exc}); "
                              f"reporting the sweep node")

    params = SabrParams(alpha, rho, nu, t, beta, forward)
    m = model(alpha, rho, nu)
    err = m - vs
    j = int(np.argmax(np.abs(err)))
    rmse = float(math.sqrt(np.mean((w * err) ** 2)))

    warnings: list[str] = []
    if held:
        warnings.append(
            f"{', '.join(_pin_labels(held))} "
            + ("was" if len(held) == 1 else "were") + " given, not fitted"
            + (", so nothing was fitted at all: the curve is the one that was typed and the "
               "residuals below are what it costs against these quotes."
               if not free else
               f". The residuals are the best {' and '.join(PIN_LABELS[k] for k in free)} "
               f"can do at {'that value' if len(held) == 1 else 'those values'}, not the best "
               f"SABR through these quotes.")
        )
    if n == len(free) and free:
        warnings.append(
            f"{n} quote{'s' if n != 1 else ''} and {len(free)} free "
            f"parameter{'s' if len(free) != 1 else ''}: this is an exact interpolation, not a "
            f"fit. The residuals will be zero whatever the quotes say, so they are no evidence "
            f"the shape is right."
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
    if "rho" not in held and abs(rho) > rho_bound - 1e-3:
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
        fixed=tuple(k for k in FIT_PARAMS if k in held),
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
    """The panel's expiry, refusing an empty one by name.

    An HTML ``datetime-local`` field emits ISO 8601 and a listed expiry always
    carries a time of day, because the exchange settles at a fixed hour.
    Reading that back is ``timeutil.parse_datetime``'s job -- it used to be
    patched here, and differently again in two other callers, which is how a
    string parsed on one screen and failed on the next.  What is left is the
    one thing this call site knows: a blank box is not a timestamp.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        raise ValueError("the panel has no expiry")
    return text


@dataclass(frozen=True)
class PanelFit:
    """What :meth:`Panel.fit_curve` found: the curve and where it was struck."""

    fit: QuoteFit
    quotes: tuple[Quote, ...]
    expiry: object
    t: float
    clock: object
    surface: object = None
    notes: tuple[str, ...] = ()


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
    # Any of the three SABR parameters given rather than fitted.  ``None`` is
    # the ordinary case and means "the fit decides"; the screen sends an empty
    # box for it.  See ``fit_sabr``.
    alpha: float | None = None
    rho: float | None = None
    volvol: float | None = None
    # How many units of the contract's base currency one option covers.  Only
    # the positions panel reads it -- a fit has no notion of size -- and it is
    # ``None`` for "whatever the contract's standard size is".
    contract_size: float | None = None

    @property
    def size(self) -> float:
        """The contract size in force: the panel's override, or the contract's."""
        if self.contract_size:
            return float(self.contract_size)
        return float(self.underlying.contract_size or 0.0)

    def fit_curve(self, book=None, *, clock=None) -> "PanelFit":
        """The curve alone: the expiry, the year fraction and the SABR fit.

        Split out of :meth:`run` because the positions panel needs exactly
        this and none of the comparison: greeks are taken on the contract's
        own curve, in the contract's own units, and never need the book.  One
        code path so a panel cannot be fitted two ways on one screen.
        """
        surface = None
        if book is not None and self.underlying.pair:
            if self.underlying.pair not in book:
                raise ValueError(
                    f"{self.underlying.pair} is not in the book; the panel's underlying "
                    f"{self.underlying.code} maps to it. Add the pair or choose CUSTOM."
                )
            surface = book[self.underlying.pair]
        # The valuation clock, in order of nearness to the thing being priced:
        # one passed in, the mapped surface's, then the book's own.  The book
        # was skipped here once, so a panel on a contract with no pair --
        # CUSTOM, or a code whose pair is not in the workbook -- refused to
        # fit at all on a screen that has a perfectly good clock: the
        # comparison is what needs a surface, not the expiry.
        the_clock = (clock
                     or (surface.clock if surface is not None else None)
                     or getattr(book, "clock", None))
        if the_clock is None:
            raise ValueError(
                "a clock is required to price a listed expiry; pass one, or a book to take "
                "the valuation time from")

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
                       table_weights=[q.weight for q in quotes],
                       fixed={"alpha": self.alpha, "rho": self.rho, "volvol": self.volvol})
        return PanelFit(fit=fit, quotes=quotes, expiry=expiry_dt, t=t,
                        clock=the_clock, surface=surface, notes=tuple(dedupe_notes))

    def run(self, book=None, *, clock=None, curve_points: int = 161) -> dict:
        """Fit, compare, and return everything the screen or the CLI needs.

        The result is plain JSON-safe data: the panel itself holds no state
        between calls, so the browser can own the list of panels and the
        server stays a pure function of what it is sent.
        """
        prep = self.fit_curve(book, clock=clock)
        surface, the_clock = prep.surface, prep.clock
        expiry_dt, t, fit, quotes = prep.expiry, prep.t, prep.fit, prep.quotes
        dedupe_notes = prep.notes

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
                # Which parameters were typed and which were fitted, so the
                # panel can show the difference rather than presenting a
                # number somebody entered as though the market implied it.
                "fixed": list(fit.fixed), "free": list(fit.free),
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


def _pin(payload: dict, key: str) -> float | None:
    """One SABR parameter override, or None for "let the fit decide".

    An empty box is not a zero.  The screen sends "" for every parameter it
    is not pinning, and reading that as a number would mark a curve nobody
    asked for -- the same rule as every other blank field on the panel.
    """
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ValueError(
            f"{PIN_LABELS.get(key, key)} must be a number or blank, got {raw!r}") from None


def panel_from_request(payload: dict) -> Panel:
    """Build a Panel from a JSON body or a CLI namespace-like mapping."""
    u = resolve_underlying(
        payload.get("underlying") or payload.get("code"),
        pair=payload.get("pair"),
        invert=(None if payload.get("invert") in (None, "") else
                str(payload["invert"]).lower() in ("1", "true", "yes", "on")),
        scale=(None if payload.get("scale") in (None, "") else float(payload["scale"])),
        contract_size=(None if payload.get("contract_size") in (None, "") else
                       float(payload["contract_size"])),
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
        alpha=_pin(payload, "alpha"),
        rho=_pin(payload, "rho"),
        volvol=_pin(payload, "volvol"),
        weighting=(payload.get("weighting") or "vega"),
        method=(payload.get("method") or None),
        cut=(payload.get("cut") or "NY"),
        label=(payload.get("label") or ""),
        notes=notes,
        # Already folded into the underlying above; carried on the panel too
        # so ``Panel.size`` is one lookup and not two.
        contract_size=(None if payload.get("contract_size") in (None, "") else
                       float(payload["contract_size"])),
    )


# ---------------------------------------------------------------------------
# positions and aggregated risk
# ---------------------------------------------------------------------------
#
# The panels above answer "what is this curve".  This answers "what do I own",
# which is the other half of an exchange-traded screen and needs nothing new
# from the market: the volatility at a strike, the forward and the expiry all
# come from the panel the position belongs to.  That is the whole design --
# a position names a panel, and every parameter the greeks need is a parameter
# that panel already had to have in order to fit.  The one thing a fit never
# needed is the *contract size*, which is why it was added to the panel above
# rather than invented here.
#
# Two sets of greeks, because they answer different questions:
#
# * **Black-Scholes** -- the closed-form Black-76 sensitivities at the option's
#   own volatility, with that volatility held fixed as the future moves.  This
#   is what a Black-Scholes greek *is*, and it is the number an exchange's own
#   risk file will agree with.
# * **Smile** -- the same position revalued on the fitted SABR curve, so the
#   curve moves with the future and the volatility at each strike is the
#   curve's own.  The premium is identical (both read the same volatility at
#   the same strike); the whole difference is in the sensitivities.
#
# Money.  A greek is turned into money by the contract size, and the CME
# settles every one of these contracts in US dollars, so the money columns add
# up across contracts and are totalled.  Futures-equivalent delta does not --
# a euro future and a yen future are different things -- so it is totalled per
# contract only, and the panel says so rather than printing a sum of unlike
# things.

_HEAD_CONTRACT = ("contract", "underlying", "code", "panel", "symbol", "product",
                  "instrument", "series")
_HEAD_EXPIRY = ("expiry", "expiration", "expiry date", "exp", "maturity", "month", "tenor")
_HEAD_QTY = ("qty", "quantity", "contracts", "lots", "size", "position", "pos", "amount",
             "net", "units")

# What the strike cell may say instead of a number.
_AT_THE_FORWARD = ("atm", "atmf", "f", "fwd", "forward", "at the money")


@dataclass(frozen=True)
class Position:
    """One line of an exchange-traded position book.

    ``strike`` is in the listed contract's own units, or ``None`` for "at the
    panel's forward".  ``underlying`` and ``expiry`` say which panel the line
    belongs to and may both be blank when the screen holds only one panel;
    resolving that is :meth:`PositionPanel.run`'s job, not the parser's, since
    only the panel list can say whether a blank is unambiguous.
    """

    strike: float | None
    is_call: bool
    quantity: float
    underlying: str = ""
    expiry: str = ""
    label: str = ""
    line: int = 0


@dataclass(frozen=True)
class ParsedPositions:
    """What :func:`parse_positions` made of a paste."""

    positions: tuple[Position, ...]
    delimiter: str
    layout: tuple[str, ...]
    notes: tuple[str, ...] = ()
    skipped: tuple[tuple[int, str, str], ...] = ()


def _kind_cell(cell: str) -> bool | None:
    """``C``/``call`` or ``P``/``put``, or None when the cell is neither."""
    s = _clean(cell).lower().rstrip(".")
    if s in ("c", "call", "calls", "cal", "co"):
        return True
    if s in ("p", "put", "puts", "po"):
        return False
    return None


def _strike_cell(cell: str) -> float | None | str:
    """A listed strike, ``None`` for at-the-forward, or an error string."""
    s = _clean(cell)
    if s.lower() in _AT_THE_FORWARD:
        return None
    v = _to_float(s)
    if v is None:
        return f"strike {s!r} is not a number"
    if v <= 0:
        return f"strike {v:g} is not positive"
    return v


_POSITION_LAYOUTS = {
    5: ("contract", "expiry", "strike", "type", "quantity"),
    4: ("expiry", "strike", "type", "quantity"),
    3: ("strike", "type", "quantity"),
}


_DELIM_NAMES = {"\t": "tab", ",": "comma", ";": "semicolon", "ws": "whitespace"}


def _detect_position_delimiter(lines: list[str]) -> str:
    """Tabs, then semicolons, then *any* comma, then whitespace.

    Deliberately not :func:`_detect_delimiter`.  A quote table is two numeric
    columns where ``1,425.00`` is a strike, so that one only believes a comma
    when every row has the same number of them.  A position table is the
    broker-run rule instead: a comma is a column boundary, full stop.  It has
    to be, because a position line legitimately varies in width -- a short
    layout, a ragged row -- and a rule that gave up on a comma the moment two
    rows disagreed would silently re-read the whole table as whitespace and
    split every timestamp in half.  A size written ``1,000`` in a comma paste
    is then two columns and its row is refused with the reason, which is the
    honest answer to an ambiguity rather than a guess at one.
    """
    if any("\t" in ln for ln in lines):
        return "\t"
    if any(";" in ln for ln in lines):
        return ";"
    if any("," in ln for ln in lines):
        return ","
    return "ws"


def parse_positions(text: str) -> ParsedPositions:
    """Read a pasted position table.  Nothing is dropped quietly.

    Either a header row naming the columns, or one of three positional
    layouts chosen by field count::

        contract, expiry, strike, C/P, contracts
                  expiry, strike, C/P, contracts
                          strike, C/P, contracts

    The layout is decided **once, from the whole table** -- the most common
    field count -- and a row that does not have that many fields is skipped
    with the reason.  Reading each row on its own width would silently move a
    quantity into a strike the first time somebody left a cell blank.

    A comma is a column boundary here as it is in a broker run -- see
    :func:`_detect_position_delimiter` -- so a size written ``1,000`` in a
    comma-separated paste is two columns and the row is refused rather than
    read as 1.  Tab- and space-separated pastes have no such ambiguity and
    ``1,000`` is a size.
    """
    raw = [(i + 1, ln) for i, ln in enumerate(text.splitlines())]
    lines = [(n, ln) for n, ln in raw if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        raise ValueError("no position lines were found; paste strike, C/P and a quantity")

    delim = _detect_position_delimiter([ln for _, ln in lines])
    rows = [(n, [c for c in _split(ln, delim)]) for n, ln in lines]

    notes: list[str] = []
    skipped: list[tuple[int, str, str]] = []
    columns: dict[str, int] | None = None

    # A header row: no digits anywhere, and at least two cells that name a
    # column we know.  Same rule as the quote table's, for the same reason --
    # a spreadsheet paste brings one and it is not an error.
    first_n, first = rows[0]
    if not any(ch.isdigit() for ch in " ".join(first)):
        found: dict[str, int] = {}
        for i, cell in enumerate(first):
            name = _clean(cell).lower()
            for key, names in (("contract", _HEAD_CONTRACT), ("expiry", _HEAD_EXPIRY),
                               ("strike", _HEAD_STRIKE), ("type", _HEAD_KIND),
                               ("quantity", _HEAD_QTY)):
                if name in names and key not in found:
                    found[key] = i
        if len(found) >= 2:
            missing = [k for k in ("strike", "type", "quantity") if k not in found]
            if missing:
                raise ValueError(
                    f"the header row names {', '.join(sorted(found))} but not "
                    f"{', '.join(missing)}; a position needs a strike, a call/put and a "
                    f"quantity")
            columns = found
            notes.append(f"header row read: " + ", ".join(
                f"{k} from column {v + 1}" for k, v in sorted(found.items(), key=lambda x: x[1])))
            rows = rows[1:]
        else:
            skipped.append((first_n, " ".join(first), "no digits and no recognised column names"))
            rows = rows[1:]

    if columns is None:
        counts: dict[int, int] = {}
        for _, cells in rows:
            counts[len(cells)] = counts.get(len(cells), 0) + 1
        if not counts:
            raise ValueError("no position lines were found once the header was removed")
        width = max(sorted(counts), key=lambda k: (counts[k], k in _POSITION_LAYOUTS))
        if width not in _POSITION_LAYOUTS:
            raise ValueError(
                f"cannot read a position table {width} columns wide; use "
                f"'contract, expiry, strike, C/P, contracts' (or drop the leading columns "
                f"when the screen has one panel), or a header row")
        layout = _POSITION_LAYOUTS[width]
        columns = {name: i for i, name in enumerate(layout)}
        notes.append(f"{width} columns, {_DELIM_NAMES.get(delim, delim)} separated, read as "
                     + ", ".join(layout))
    else:
        layout = tuple(k for k, _ in sorted(columns.items(), key=lambda x: x[1]))
        width = None

    out: list[Position] = []
    for n, cells in rows:
        text_line = (delim if delim != "ws" else " ").join(cells).strip()
        need = max(columns.values()) + 1
        if width is not None and len(cells) != width:
            skipped.append((n, text_line, f"{len(cells)} columns, the table is {width}"))
            continue
        if len(cells) < need:
            skipped.append((n, text_line, f"{len(cells)} columns, the header needs {need}"))
            continue
        k = _strike_cell(cells[columns["strike"]])
        if isinstance(k, str):
            skipped.append((n, text_line, k))
            continue
        cp = _kind_cell(cells[columns["type"]])
        if cp is None:
            skipped.append((n, text_line,
                            f"{_clean(cells[columns['type']])!r} is not a call or a put"))
            continue
        q = _to_float(cells[columns["quantity"]])
        if q is None:
            skipped.append((n, text_line,
                            f"quantity {_clean(cells[columns['quantity']])!r} is not a number"))
            continue
        if not math.isfinite(q):
            skipped.append((n, text_line, f"quantity {q!r} is not finite"))
            continue
        out.append(Position(
            strike=k, is_call=cp, quantity=float(q),
            underlying=(_clean(cells[columns["contract"]]) if "contract" in columns else ""),
            expiry=(_clean(cells[columns["expiry"]]) if "expiry" in columns else ""),
            line=n))
    if not out:
        raise ValueError(
            "no position line could be read; " +
            (skipped[0][2] if skipped else "the table is empty"))
    return ParsedPositions(tuple(out), delim, tuple(layout), tuple(notes), tuple(skipped))


# The greek names, in the order the screen and the CLI show them, and what
# each one is measured in.  Declared once so a column cannot be added to the
# table without a unit beside it.
GREEK_FIELDS: tuple[tuple[str, str], ...] = (
    ("delta_futures", "futures equivalent"),
    ("delta_1pct", "money, +1% of the future"),
    ("gamma_futures", "futures per 1.00 of the future"),
    ("gamma_1pct", "money, curvature over 1%"),
    ("vega", "money per vol bump"),
    ("theta", "money over the theta window"),
    ("vanna_1pct", "delta money per vol bump"),
    ("volga", "money per vol bump squared"),
)

# Which of those may be added across contracts.  Money may -- every CME FX
# option settles in US dollars.  A futures-equivalent may not: a euro future
# and a yen future are different instruments and their sum is not a number.
ADDITIVE_GREEKS: tuple[str, ...] = tuple(
    k for k, _ in GREEK_FIELDS if k not in ("delta_futures", "gamma_futures"))

# Relative bumps for the revalued (smile) greeks.  Both are far above the
# noise of a double and far below where the third derivative starts to show.
_F_BUMP = 1e-4          # of the forward
_ALPHA_BUMP = 1e-3      # of alpha, converted to a volatility move before use


def _blank_greeks() -> dict[str, float | None]:
    return {k: None for k, _ in GREEK_FIELDS}


def _money_greeks(*, delta, gamma, vega, theta, vanna, volga,
                  forward: float, units: float, qty: float,
                  vol_bump: float, theta_years: float) -> dict[str, float]:
    """Turn per-unit, per-1.00-of-volatility greeks into the table's columns.

    ``units`` is the position's exposure in units of the contract's base
    currency (contracts times contract size); ``qty`` is the contract count,
    which is what a futures-equivalent is quoted in.
    """
    step = 0.01 * forward
    bump = vol_bump / 100.0                     # vol points -> volatility
    return {
        "delta_futures": delta * qty,
        "delta_1pct": delta * step * units,
        "gamma_futures": gamma * qty,
        "gamma_1pct": 0.5 * gamma * step * step * units,
        "vega": vega * units * bump,
        "theta": theta * units * theta_years,
        "vanna_1pct": vanna * step * units * bump,
        "volga": volga * units * bump * bump,
    }


def _smile_price(params: SabrParams, K: float, is_call: bool) -> float:
    """Revalue one option on a SABR curve, reading its own volatility off it."""
    v = float(lognormal_vol(K, params))
    return float(black.price(params.f, K, v, params.t, is_call))


def _smile_delta(params: SabrParams, K: float, is_call: bool) -> tuple[float, float, float]:
    """Delta, gamma and the base price by revaluation, with the curve moving.

    The forward is bumped *inside* the SABR parameters as well as in Black, so
    the curve travels with the future rather than staying pinned to the old
    one.  That is the model's own answer to "what happens if the future
    moves", and it is the whole difference between these and the frozen-
    volatility numbers beside them.
    """
    h = params.f * _F_BUMP
    up = replace(params, f=params.f + h)
    dn = replace(params, f=params.f - h)
    pv = _smile_price(params, K, is_call)
    pv_up = _smile_price(up, K, is_call)
    pv_dn = _smile_price(dn, K, is_call)
    return (pv_up - pv_dn) / (2.0 * h), (pv_up - 2.0 * pv + pv_dn) / (h * h), pv


def smile_greeks(params: SabrParams, K: float, is_call: bool,
                 theta_years: float) -> dict[str, float]:
    """Every smile greek for one option, by revaluation on the fitted curve.

    Volatility is bumped by scaling ``alpha``, which lifts the whole curve,
    and the move is then *measured* at the forward and divided out -- so the
    number reported is per one unit of at-the-money volatility however alpha
    happens to map onto it at this expiry.  Scaling alpha rather than solving
    for a target at-the-money keeps this free of a solve, and to first order
    the two are the same shift.
    """
    delta, gamma, pv = _smile_delta(params, K, is_call)

    up = params.with_alpha(params.alpha * (1.0 + _ALPHA_BUMP))
    dn = params.with_alpha(params.alpha * (1.0 - _ALPHA_BUMP))
    dv = (atm_vol(up) - atm_vol(dn)) / 2.0
    if not (dv > 0):
        raise ConvergenceError(
            f"scaling alpha by {_ALPHA_BUMP:g} moved the at-the-money volatility by "
            f"{dv:.3g}, so vega cannot be normalised; the fitted curve is degenerate")
    pv_up, pv_dn = _smile_price(up, K, is_call), _smile_price(dn, K, is_call)
    vega = (pv_up - pv_dn) / (2.0 * dv)
    volga = (pv_up - 2.0 * pv + pv_dn) / (dv * dv)
    d_up, _, _ = _smile_delta(up, K, is_call)
    d_dn, _, _ = _smile_delta(dn, K, is_call)
    vanna = (d_up - d_dn) / (2.0 * dv)

    t2 = params.t - theta_years
    if t2 <= 0:
        theta = None
    else:
        theta = (_smile_price(replace(params, t=t2), K, is_call) - pv) / theta_years
    return {"price": pv, "delta": delta, "gamma": gamma, "vega": vega,
            "theta": theta, "vanna": vanna, "volga": volga}


@dataclass(frozen=True)
class _Entry:
    """One fit panel, as the positions panel sees it."""

    index: int
    label: str
    code: str
    panel: Panel
    prep: PanelFit | None = None
    error: str = ""

    @property
    def name(self) -> str:
        return self.label or f"{self.code} #{self.index + 1}"


def _entry_expiry(e: _Entry):
    return e.prep.expiry if e.prep is not None else None


def _match_panel(pos: Position, entries: list[_Entry], clock) -> _Entry:
    """Which panel a position belongs to, or a refusal that says why.

    A position may name a panel by its label or by its contract code, and may
    name an expiry; whatever it leaves out has to be unambiguous among what is
    on the screen.  Guessing here would be the worst possible failure -- a
    position priced against the wrong month's curve looks perfectly ordinary.
    """
    if not entries:
        raise ValueError("there are no panels on the screen to price this against; "
                         "add the contract and expiry as a panel first")
    cands = entries
    if pos.underlying:
        key = pos.underlying.strip().upper()
        # The display name too, because that is what the refusal below offers
        # back and a name it prints has to be a name it accepts.
        cands = [e for e in entries
                 if key in (e.label.strip().upper(), e.code.strip().upper(),
                            e.name.strip().upper())]
        if not cands:
            raise ValueError(
                f"no panel is labelled or contracted {pos.underlying!r}; the screen has "
                f"{', '.join(sorted({e.name for e in entries}))}")
    live = [e for e in cands if e.prep is not None]
    if not live:
        raise ValueError(f"the panel {cands[0].name} did not fit: {cands[0].error}")
    if pos.expiry:
        want = clock.coerce_datetime(_normalise_expiry(pos.expiry))
        dated = [e for e in live if _entry_expiry(e) == want]
        if not dated:
            raise ValueError(
                f"no panel expires at {want:%Y-%m-%d %H:%M}Z; the ones that could be used "
                f"expire " + ", ".join(sorted({f"{_entry_expiry(e):%Y-%m-%d %H:%M}Z"
                                               for e in live})))
        live = dated
    if len(live) > 1:
        raise ValueError(
            f"this line matches {len(live)} panels ({', '.join(e.name for e in live)}); "
            f"name the contract and the expiry so it can only mean one of them")
    return live[0]


@dataclass
class PositionPanel:
    """A book of exchange-traded positions, priced against the screen's panels.

    The browser owns the panels and the paste and posts both whole, so this is
    a pure function of its request and ``volkit listed --positions`` reproduces
    a screen exactly -- the same discipline as every other panel here.
    """

    positions: tuple[Position, ...]
    panels: tuple[Panel, ...] = ()
    vol_bump: float = 1.0               # volatility points
    theta_days: float = 1.0
    notes: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[tuple[int, str, str], ...] = ()

    def run(self, book=None, *, clock=None) -> dict:
        if not (self.vol_bump > 0):
            raise ValueError(f"the vol bump must be positive, got {self.vol_bump!r} vol points")
        if not (self.theta_days > 0):
            raise ValueError(f"the theta window must be positive, got {self.theta_days!r} days")
        theta_years = self.theta_days / 365.2425

        # Fit every panel once.  A panel that will not fit does not take the
        # rest of the book down with it: its own positions report its message
        # and every other line is still priced.
        entries: list[_Entry] = []
        for i, panel in enumerate(self.panels):
            code = panel.underlying.code
            try:
                prep = panel.fit_curve(None, clock=clock)
            except Exception as exc:  # noqa: BLE001 - carried onto the rows it owns
                entries.append(_Entry(i, panel.label, code, panel,
                                      error=f"{type(exc).__name__}: {exc}"))
            else:
                entries.append(_Entry(i, panel.label, code, panel, prep=prep))

        the_clock = clock or getattr(book, "clock", None)
        if the_clock is None:
            the_clock = next((e.prep.clock for e in entries if e.prep is not None), None)
        if the_clock is None:
            raise ValueError("a clock is required to place a listed expiry; pass one, or a book")

        notes = list(self.notes)
        warnings: list[str] = []
        rows, groups = [], {}
        sized = {e.index: (e.panel.size or 0.0) for e in entries}
        for e in entries:
            if e.prep is not None and not sized[e.index]:
                warnings.append(
                    f"{e.name} has no contract size, so its money columns are per one unit of "
                    f"the base currency. Set 'Contract size' on that panel to get money.")

        for pos in self.positions:
            row = {
                "line": pos.line, "label": pos.label,
                "panel": "", "underlying": pos.underlying, "expiry": "",
                "strike": pos.strike, "fx_strike": None,
                "type": "call" if pos.is_call else "put",
                "quantity": pos.quantity, "contract_size": None,
                "forward": None, "years": None, "vol": None,
                "premium": None, "premium_unit": None,
                "bs": _blank_greeks(), "smile": _blank_greeks(), "error": "",
            }
            try:
                e = _match_panel(pos, entries, the_clock)
            except (ValueError, KeyError) as exc:
                row["error"] = str(exc)
                rows.append(row)
                continue

            prep, params = e.prep, e.prep.fit.params
            size = sized[e.index] or 1.0
            units = pos.quantity * size
            K = params.f if pos.strike is None else float(pos.strike)
            row.update({"panel": e.name, "underlying": e.code, "contract_size": size,
                        "expiry": prep.expiry.isoformat(), "forward": params.f,
                        "years": prep.t, "strike": K})
            try:
                if K <= 0:
                    raise ValueError(f"strike {K:g} is not positive")
                vol = float(lognormal_vol(K, params))
                pv = float(black.price(params.f, K, vol, params.t, pos.is_call))
                bs = _money_greeks(
                    delta=float(black.delta(params.f, K, vol, params.t, pos.is_call)),
                    gamma=float(black.gamma(params.f, K, vol, params.t)),
                    vega=float(black.vega(params.f, K, vol, params.t)),
                    theta=float(black.theta(params.f, K, vol, params.t)),
                    vanna=float(black.vanna(params.f, K, vol, params.t)),
                    volga=float(black.volga(params.f, K, vol, params.t)),
                    forward=params.f, units=units, qty=pos.quantity,
                    vol_bump=self.vol_bump, theta_years=theta_years)
                sm = smile_greeks(params, K, pos.is_call, theta_years)
                smile = _money_greeks(
                    delta=sm["delta"], gamma=sm["gamma"], vega=sm["vega"],
                    theta=(sm["theta"] if sm["theta"] is not None else 0.0),
                    vanna=sm["vanna"], volga=sm["volga"],
                    forward=params.f, units=units, qty=pos.quantity,
                    vol_bump=self.vol_bump, theta_years=theta_years)
                if sm["theta"] is None:
                    # The window reaches past the expiry, so there is no
                    # revaluation to take the decay from.  Blank, with the
                    # reason, rather than a plausible number.
                    smile["theta"] = None
                    row["error"] = (f"the {self.theta_days:g}-day theta window reaches past "
                                    f"this expiry, so smile theta is not reported")
                row.update({"vol": vol * 100.0, "premium_unit": pv, "premium": pv * units,
                            "bs": bs, "smile": smile})
                if e.panel.underlying.pair:
                    row["fx_strike"] = float(e.panel.underlying.to_fx(K))
            except (ValueError, ZeroDivisionError, ConvergenceError) as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
                continue

            g = groups.setdefault(e.index, {
                "panel": e.name, "underlying": e.code, "pair": e.panel.underlying.pair,
                "expiry": prep.expiry.isoformat(), "forward": params.f,
                "contract_size": size, "n": 0, "premium": 0.0,
                "atm_vol": float(atm_vol(params)) * 100.0,
                "bs": {k: 0.0 for k, _ in GREEK_FIELDS},
                "smile": {k: 0.0 for k, _ in GREEK_FIELDS},
            })
            g["n"] += 1
            g["premium"] += row["premium"]
            for which in ("bs", "smile"):
                for key, _ in GREEK_FIELDS:
                    v = row[which][key]
                    if v is not None:
                        g[which][key] += v
            rows.append(row)

        totals = {"premium": 0.0,
                  "bs": {k: 0.0 for k in ADDITIVE_GREEKS},
                  "smile": {k: 0.0 for k in ADDITIVE_GREEKS}}
        for g in groups.values():
            totals["premium"] += g["premium"]
            for which in ("bs", "smile"):
                for key in ADDITIVE_GREEKS:
                    totals[which][key] += g[which][key]

        codes = {g["underlying"] for g in groups.values()}
        if len(codes) > 1:
            notes.append(
                f"{len(codes)} contracts ({', '.join(sorted(codes))}). The money columns are "
                f"totalled -- every CME FX option settles in US dollars -- and the "
                f"futures-equivalent delta and gamma are not, because a future of one contract "
                f"is not a future of another. Those two are per contract only.")
        failed = [r for r in rows if r["error"] and r["bs"]["vega"] is None]
        if failed:
            warnings.append(f"{len(failed)} of {len(rows)} position line(s) could not be priced; "
                            f"they keep their place in the table with the reason.")

        return {
            "valuation": the_clock.now.isoformat(),
            "vol_bump": self.vol_bump,
            "theta_days": self.theta_days,
            "greek_fields": [{"key": k, "unit": u} for k, u in GREEK_FIELDS],
            "additive": list(ADDITIVE_GREEKS),
            "positions": rows,
            "groups": [groups[k] for k in sorted(groups)],
            "totals": totals,
            "panels": [{"index": e.index, "name": e.name, "underlying": e.code,
                        "pair": e.panel.underlying.pair,
                        "ok": e.prep is not None, "error": e.error,
                        "contract_size": sized[e.index],
                        "expiry": (e.prep.expiry.isoformat() if e.prep is not None else ""),
                        "forward": (e.prep.fit.params.f if e.prep is not None else None),
                        "atm_vol": (float(atm_vol(e.prep.fit.params)) * 100.0
                                    if e.prep is not None else None),
                        "used": groups.get(e.index, {}).get("n", 0)}
                       for e in entries],
            "notes": notes,
            "warnings": warnings,
            "skipped": [{"line": n, "text": txt[:80], "why": why}
                        for n, txt, why in self.skipped],
        }


@dataclass(frozen=True)
class _BrokenPanel:
    """A panel the screen is showing that will not even build.

    It has to keep its place: a position naming it must be told what is wrong
    with that panel, not that no such panel exists.  It answers the two things
    the positions panel asks of a panel before fitting -- what it is called
    and what it is -- and raises the original message when asked to fit.
    """

    label: str
    code: str
    message: str

    @property
    def underlying(self) -> ListedUnderlying:
        return UNDERLYINGS.get(self.code) or UNDERLYINGS["CUSTOM"]

    @property
    def size(self) -> float:
        return 0.0

    def fit_curve(self, book=None, *, clock=None):
        raise ValueError(self.message)


def _position_from_row(r: dict, i: int) -> Position:
    """One structured position row, for a caller that posts them already read.

    A call/put that cannot be read is refused rather than defaulted: a short
    put booked as a long call is not a rounding error.
    """
    if "is_call" in r:
        is_call = bool(r["is_call"])
    else:
        is_call = _kind_cell(str(r.get("type", "")))
        if is_call is None:
            raise ValueError(
                f"position {i + 1} has no call/put: {r.get('type')!r} is neither")
    strike = r.get("strike")
    if isinstance(strike, str):
        strike = _strike_cell(strike)
        if isinstance(strike, str):
            raise ValueError(f"position {i + 1}: {strike}")
    return Position(
        strike=(None if strike in (None, "") else float(strike)),
        is_call=is_call,
        quantity=float(r.get("quantity", 0.0) or 0.0),
        underlying=str(r.get("underlying") or r.get("contract") or ""),
        expiry=str(r.get("expiry") or ""),
        label=str(r.get("label") or ""),
        line=int(r.get("line") or (i + 1)),
    )


def positions_from_request(payload: dict) -> PositionPanel:
    """Build a :class:`PositionPanel` from a JSON body or a CLI mapping.

    ``panels`` are the fit panels the screen is showing, sent whole and rebuilt
    here through :func:`panel_from_request` -- so a panel that will not even
    build (no forward, an unreadable table) becomes one failed entry rather
    than a 400 that empties the whole positions screen.
    """
    if payload.get("positions"):
        positions = tuple(_position_from_row(r, i) for i, r in enumerate(payload["positions"]))
        notes: tuple[str, ...] = ()
        skipped: tuple[tuple[int, str, str], ...] = ()
    else:
        parsed = parse_positions(payload.get("text", "") or "")
        positions, notes, skipped = parsed.positions, parsed.notes, parsed.skipped
        notes = (f"{len(positions)} position line(s) read",) + notes

    panels = []
    for i, spec in enumerate(payload.get("panels") or []):
        try:
            panels.append(panel_from_request(spec))
        except Exception as exc:  # noqa: BLE001 - one bad panel is not the whole screen
            # A panel that cannot even be built still has to exist, or a
            # position naming it would be told there is no such panel rather
            # than being told what is wrong with it.
            panels.append(_BrokenPanel(
                label=str(spec.get("label") or ""),
                code=str(spec.get("underlying") or spec.get("code") or "CUSTOM").strip().upper(),
                message=f"{type(exc).__name__}: {exc}"))
    return PositionPanel(
        positions=positions,
        panels=tuple(panels),
        vol_bump=float(payload.get("vol_bump") or 1.0),
        theta_days=float(payload.get("theta_days") or 1.0),
        notes=notes,
        skipped=skipped,
    )
