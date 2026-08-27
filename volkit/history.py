"""Historical spot, forwards and quoted volatility, and what actually happened.

The analysis screen needs a second workbook: one sheet per pair, one row per
past date, columns holding spot, forward points or outrights, and the quoted
ATM, risk reversal and butterfly at each tenor.  Nobody keeps that file in a
fixed layout, so the columns are matched by reading their headers rather than
by position -- ``ATM 1M``, ``1m atm vol``, ``RR25 3M`` and ``3M 25d rr`` all
land in the right place, and anything that cannot be understood is reported
with the header that confused it.

The statistics side answers one question: *did the market deliver what it was
charging for?*  That needs realized volatility to be measured on the same
footing as implied, which is the part usually got wrong.  A quoted volatility
from this book is a volatility per unit of **volatility time** -- weekends
count for almost nothing, holidays for less than a full day, and the intraday
profile is not flat.  Annualising realized returns by calendar days, or by a
flat 252, compares them against a different clock.  So the default here
divides the realized sum of squares by the same weighted time the model uses
to integrate variance, and the two naive alternatives are computed alongside
so the difference is visible rather than assumed away.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .calendars import CalendarSet, DEFAULT_CALENDARS
from .marketdata import open_workbook
from .timeutil import UTC, DAYS_IN_YEAR, TenorError, parse_tenor, tenor_to_years
from .timeweight import TimeWeighting

ANNUALISATIONS = ("weighted", "calendar", "count")
RETURN_BASES = ("spot", "forward", "auto")
BUSINESS_DAYS_PER_YEAR = 252.0

# Header words, after normalisation, and the field they name.
_FIELD_WORDS: dict[str, str] = {
    "spot": "spot", "px": "spot", "close": "spot", "last": "spot", "fix": "spot",
    "fwd": "forward", "forward": "forward", "outright": "forward", "fwdrate": "forward",
    "pts": "points", "points": "points", "swap": "points", "fwdpts": "points",
    "swappoints": "points",
    "atm": "atm", "vol": "atm", "iv": "atm", "sigma": "atm", "atmvol": "atm",
    "impvol": "atm", "impliedvol": "atm",
    "rr": "rr", "riskreversal": "rr", "rrr": "rr",
    "bf": "bf", "fly": "bf", "butterfly": "bf", "str": "bf", "strangle": "bf",
    "stg": "bf", "smile": "bf",
}
_DATE_WORDS = {"date", "dates", "asof", "as of", "valuation", "day", "time", "dt"}
_SPLIT = re.compile(r"[^a-z0-9]+")
_DELTA = re.compile(r"^(\d{1,2})d?$")


class HistoryError(ValueError):
    """Raised when the historical workbook cannot be interpreted."""


@dataclass(frozen=True)
class Column:
    """What one header was understood to mean."""

    header: str
    field: str                       # spot | forward | points | atm | rr | bf
    tenor: str | None = None
    delta: int | None = None

    @property
    def key(self) -> str:
        if self.field in ("spot",):
            return "spot"
        if self.field in ("rr", "bf"):
            return f"{self.field}{self.delta}"
        return self.field


# Deltas are quoted at these values and nowhere else.  The rule matters: a
# header like "RR 10d 1M" has two tokens that both parse as tenors, and
# reading the 10d as a ten-day tenor rather than a ten-delta wing silently
# files the whole column under a maturity that does not exist.
_DELTA_VALUES = frozenset({5, 10, 15, 20, 25, 30, 35, 40, 45})


def parse_header(header: str) -> Column | None:
    """Read one column header into a field, a tenor and a delta.

    Tokens are classified first and assigned afterwards, rather than in the
    order they appear.  Header word order is not something a spreadsheet
    guarantees -- ``RR 10d 1M`` and ``1M 25d RR`` both occur -- and deciding
    what the second token means before seeing the third gets the first of
    those wrong.

    Returns ``None`` when nothing in the header names a field this module
    knows; the caller reports those rather than dropping them, because a
    column silently ignored is a series silently missing from the analysis.
    """
    raw = str(header).strip()
    tokens = [tk for tk in _SPLIT.split(raw.lower()) if tk]
    if not tokens:
        return None

    field_name: str | None = None
    glued_delta: int | None = None
    plain: list[str] = []
    for tk in tokens:
        if field_name is None and tk in _FIELD_WORDS:
            field_name = _FIELD_WORDS[tk]
            continue
        if field_name is None:
            # "rr25" and "25rr" carry the field and the delta in one token.
            hit = False
            for word, name in _FIELD_WORDS.items():
                for rest in (tk[len(word):] if tk.startswith(word) else None,
                             tk[:-len(word)] if tk.endswith(word) else None):
                    if rest is None or not rest:
                        continue
                    m = _DELTA.match(rest)
                    if m and int(m.group(1)) in _DELTA_VALUES:
                        field_name, glued_delta, hit = name, int(m.group(1)), True
                        break
                if hit:
                    break
            if hit:
                continue
        plain.append(tk)
    if field_name is None:
        return None

    def as_tenor(tk: str) -> str | None:
        try:
            parse_tenor(tk)
        except TenorError:
            return None
        return tk.upper()

    def as_delta(tk: str) -> int | None:
        m = _DELTA.match(tk)
        if not m:
            return None
        v = int(m.group(1))
        return v if v in _DELTA_VALUES else None

    delta = glued_delta
    tenor: str | None = None
    wants_delta = field_name in ("rr", "bf") and delta is None
    remaining = list(plain)

    if wants_delta:
        # Prefer a token that can *only* be a delta; fall back to one that
        # could be either, but only when another token can carry the tenor.
        only_delta = [tk for tk in remaining if as_delta(tk) is not None and as_tenor(tk) is None]
        both = [tk for tk in remaining if as_delta(tk) is not None and as_tenor(tk) is not None]
        pick = None
        if only_delta:
            pick = only_delta[0]
        elif both and any(as_tenor(tk) is not None for tk in remaining if tk not in both[:1]):
            pick = both[0]
        if pick is not None:
            delta = as_delta(pick)
            remaining.remove(pick)

    for tk in remaining:
        got = as_tenor(tk)
        if got is not None:
            tenor = got
            break

    if field_name in ("rr", "bf") and delta is None:
        delta = 25                    # the desk default, and it is reported
    if field_name in ("forward", "points", "atm", "rr", "bf") and tenor is None:
        return None
    return Column(header=raw, field=field_name, tenor=tenor, delta=delta)


def _is_date_header(header) -> bool:
    return str(header).strip().lower().replace("_", " ") in _DATE_WORDS


@dataclass
class PairHistory:
    """One sheet: dates, and every series that could be read off it."""

    pair: str
    dates: list[date] = field(default_factory=list)
    spot: np.ndarray = field(default_factory=lambda: np.empty(0))
    forwards: dict[str, np.ndarray] = field(default_factory=dict)   # outrights
    atm: dict[str, np.ndarray] = field(default_factory=dict)
    rr: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)   # rr["25"]["1M"]
    bf: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    columns: list[Column] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def tenors(self) -> list[str]:
        seen = set(self.atm) | set(self.forwards)
        for book in (self.rr, self.bf):
            for by_tenor in book.values():
                seen |= set(by_tenor)
        return sorted(seen, key=tenor_to_years)

    def window(self, lookback_days: float, end: date | None = None) -> tuple[int, int]:
        """Index range ``[i, j)`` covering the last ``lookback_days`` calendar days."""
        if not self.dates:
            raise HistoryError(f"{self.pair}: no dated rows")
        last = end or self.dates[-1]
        first = last - timedelta(days=float(lookback_days))
        i = int(np.searchsorted(np.array(self.dates), first, side="left"))
        j = int(np.searchsorted(np.array(self.dates), last, side="right"))
        return i, j

    def series(self, field_name: str, tenor: str, delta: int = 25) -> np.ndarray | None:
        key = tenor.upper()
        if field_name == "atm":
            return self.atm.get(key)
        if field_name == "forward":
            return self.forwards.get(key)
        book = self.rr if field_name == "rr" else self.bf if field_name == "bf" else None
        if book is None:
            return None
        return book.get(str(delta), {}).get(key)


@dataclass
class History:
    """Every sheet in a historical workbook."""

    pairs: dict[str, PairHistory] = field(default_factory=dict)
    source: str = ""
    problems: list[str] = field(default_factory=list)
    skipped_sheets: list[str] = field(default_factory=list)

    def __contains__(self, pair: str) -> bool:
        return pair.upper() in self.pairs

    def __getitem__(self, pair: str) -> PairHistory:
        key = pair.upper()
        if key not in self.pairs:
            raise HistoryError(
                f"no history for {pair!r}; the workbook has {sorted(self.pairs) or 'no readable sheets'}"
            )
        return self.pairs[key]

    def summary(self) -> list[dict]:
        return [{
            "pair": h.pair, "rows": len(h.dates),
            "from": h.dates[0].isoformat() if h.dates else "",
            "to": h.dates[-1].isoformat() if h.dates else "",
            "tenors": h.tenors,
            "has_spot": bool(h.spot.size),
            "problems": len(h.problems),
        } for h in self.pairs.values()]


def _sheet_to_pair(name: str, known: set[str] | None) -> str | None:
    flat = re.sub(r"[^A-Za-z]", "", str(name)).upper()
    if known:
        for p in known:
            if flat.startswith(p):
                return p
    return flat[:6] if len(flat) >= 6 else None


VOL_UNITS = ("auto", "percent", "decimal")


def load_history(path: str | Path, known_pairs=None, *, vol_unit: str = "auto") -> History:
    """Read a historical workbook: one sheet per pair, one row per date.

    ``vol_unit`` says whether the volatility columns are quoted in points
    (``9.25``) or decimals (``0.0925``).  On ``auto`` it is decided **once per
    sheet, from the at-the-money columns**, and the same scale is then applied
    to the risk reversals and butterflies.  Deciding it column by column is
    what a first cut of this did, and it is wrong: a 25 delta risk reversal of
    -0.89 vol points is below 1 in magnitude, so it looks exactly like a
    decimal and comes through a hundred times too large.  The at-the-money
    level is never ambiguous, so it is what decides.
    """
    if vol_unit not in VOL_UNITS:
        raise ValueError(f"unknown volatility unit {vol_unit!r}; expected one of {VOL_UNITS}")
    path = Path(path)
    if not path.exists():
        raise HistoryError(f"historical workbook not found: {path}")
    known = {p.upper() for p in known_pairs} if known_pairs else None
    try:
        book = open_workbook(path)
    except Exception as exc:  # noqa: BLE001 - reported with the file name
        raise HistoryError(f"could not open {path}: {type(exc).__name__}: {exc}") from None

    # ``with``, because this workbook is the one a user is most likely to have
    # open in Excel at the same time: it is their own history sheet, and the
    # tool only reads it.  A reader left alive here is what stopped them
    # saving it -- see ``marketdata.open_workbook``.
    out = History(source=str(path))
    with book:
        for sheet in book.sheet_names:
            pair = _sheet_to_pair(sheet, known)
            if pair is None:
                out.skipped_sheets.append(f"{sheet!r}: the sheet name does not look like a pair")
                continue
            try:
                hist = _read_sheet(book, sheet, pair, vol_unit)
            except HistoryError as exc:
                out.skipped_sheets.append(f"{sheet!r}: {exc}")
                continue
            if pair in out.pairs:
                out.problems.append(f"{pair}: more than one sheet maps to it; kept {sheet!r}")
            out.pairs[pair] = hist
            out.problems.extend(f"{pair}: {p}" for p in hist.problems)
    if not out.pairs:
        raise HistoryError(
            f"{path}: no sheet could be read as a pair history. "
            + ("; ".join(out.skipped_sheets[:4]) if out.skipped_sheets else "the file has no sheets")
        )
    return out


def _read_sheet(book: pd.ExcelFile, sheet: str, pair: str, vol_unit: str = "auto") -> PairHistory:
    df = book.parse(sheet)
    if df.empty:
        raise HistoryError("the sheet has no rows")
    date_col = next((c for c in df.columns if _is_date_header(c)), None)
    if date_col is None:
        date_col = df.columns[0]
    dates_raw = pd.to_datetime(df[date_col], errors="coerce")
    good = dates_raw.notna()
    if not good.any():
        raise HistoryError(f"column {date_col!r} holds no readable dates")
    df = df.loc[good].copy()
    order = np.argsort(dates_raw[good].values)
    df = df.iloc[order]
    dates = [pd.Timestamp(d).date() for d in dates_raw[good].values[order]]

    hist = PairHistory(pair=pair, dates=dates)
    if len(set(dates)) != len(dates):
        hist.problems.append(f"{len(dates) - len(set(dates))} duplicate date(s); all rows kept")

    staged: list[tuple[Column, np.ndarray]] = []
    for col in df.columns:
        if col is date_col:
            continue
        spec = parse_header(col)
        if spec is None:
            hist.problems.append(f"column {str(col)!r} was not understood and is unused")
            continue
        values = pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)
        if np.all(np.isnan(values)):
            hist.problems.append(f"column {str(col)!r} holds no numbers")
            continue
        staged.append((spec, values))

    divisor, why = _vol_divisor(staged, vol_unit)
    if why:
        hist.problems.append(why)
    for spec, values in staged:
        hist.columns.append(spec)
        if spec.field == "spot":
            hist.spot = values
        elif spec.field == "forward":
            hist.forwards[spec.tenor] = values
        elif spec.field == "points":
            hist.forwards.setdefault(f"_pts_{spec.tenor}", values)
        elif spec.field == "atm":
            hist.atm[spec.tenor] = values / divisor
        elif spec.field in ("rr", "bf"):
            book_ = hist.rr if spec.field == "rr" else hist.bf
            book_.setdefault(str(spec.delta), {})[spec.tenor] = values / divisor

    # Forward points become outrights once spot is known; doing it here rather
    # than at use means the rest of the module only ever sees outrights.
    pts = {k[len("_pts_"):]: v for k, v in list(hist.forwards.items()) if k.startswith("_pts_")}
    for key in list(hist.forwards):
        if key.startswith("_pts_"):
            del hist.forwards[key]
    if pts:
        if not hist.spot.size:
            hist.problems.append(
                f"forward points given for {', '.join(sorted(pts))} but there is no spot column, "
                f"so they cannot be turned into outrights"
            )
        else:
            from .feed import pip_divisor
            pip = pip_divisor(pair)
            for tenor, values in pts.items():
                if tenor in hist.forwards:
                    hist.problems.append(f"{tenor}: both an outright and points were given; kept the outright")
                    continue
                hist.forwards[tenor] = hist.spot + values / pip
    if not hist.spot.size and not hist.atm:
        raise HistoryError("neither a spot column nor any volatility column was found")
    return hist


def _vol_divisor(staged, vol_unit: str) -> tuple[float, str]:
    """One scale for every volatility column on the sheet, and why.

    The at-the-money level decides: a quoted ATM is somewhere between 2 and 60
    in points and between 0.02 and 0.60 in decimals, and nothing sensible sits
    near 1.  Risk reversals and butterflies are then scaled to match, rather
    than being sniffed individually where a small one would be misread.
    """
    if vol_unit == "percent":
        return 100.0, ""
    if vol_unit == "decimal":
        return 1.0, ""
    atm = [v for spec, v in staged if spec.field == "atm"]
    if atm:
        finite = np.concatenate([v[np.isfinite(v)] for v in atm]) if atm else np.empty(0)
        if finite.size:
            level = float(np.median(np.abs(finite)))
            if level > 1.0:
                return 100.0, ""
            if 0.0 < level < 1.0:
                return 1.0, ""
    shaped = [v for spec, v in staged if spec.field in ("rr", "bf")]
    if not shaped:
        return 100.0, ""
    return 100.0, (
        "no at-the-money column, so the volatility unit could not be determined from an "
        "unambiguous number; the risk reversals and butterflies were read as vol points. "
        "Load with vol_unit='decimal' if that is wrong"
    )


# ---------------------------------------------------------------------------
# what actually happened
# ---------------------------------------------------------------------------

def forward_series(hist: PairHistory, tenor: str) -> tuple[np.ndarray | None, str]:
    """The outright forward series at ``tenor``, interpolated if it is not quoted.

    A historical sheet holds a handful of pillars and a workbook holds nine
    tenors, so asking for the exact one usually misses.  Falling back to spot
    on the misses and to the forward on the hits was the first cut, and it is
    the worse answer: it puts two different measurements in one column, so the
    term structure of realized volatility develops steps at whichever tenors
    the sheet happens to quote.

    So the carry is interpolated instead, the same way ``feed.py`` interpolates
    a live curve: linearly in time on ``log(F/S)``, which is the swap points as
    a ratio, held flat beyond the last pillar and scaled to zero below the
    first.  Returns ``(series, note)``; the note is empty when the tenor was
    quoted outright and says what was done otherwise.
    """
    key = tenor.upper()
    exact = hist.forwards.get(key)
    if exact is not None:
        return exact, ""
    pillars = [(tenor_to_years(k), v) for k, v in hist.forwards.items()]
    pillars = [(t, v) for t, v in pillars if t > 0]
    if not pillars or not hist.spot.size:
        return None, ""
    pillars.sort(key=lambda z: z[0])
    tau = tenor_to_years(key)
    ts = np.array([t for t, _ in pillars])
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.vstack([np.log(np.where(v > 0, v, np.nan) / hist.spot) for _, v in pillars])
    if tau <= ts[0]:
        # Swap points vanish at zero time, so the front pillar is scaled down
        # rather than held flat across the very short end.
        out = ratios[0] * (tau / ts[0])
        where = f"scaled down from the {_name_of(hist, ts[0])} pillar"
    elif tau >= ts[-1]:
        out = ratios[-1]
        where = f"held flat from the {_name_of(hist, ts[-1])} pillar"
    else:
        j = int(np.searchsorted(ts, tau))
        w = (tau - ts[j - 1]) / (ts[j] - ts[j - 1])
        out = (1.0 - w) * ratios[j - 1] + w * ratios[j]
        where = (f"interpolated between the {_name_of(hist, ts[j - 1])} and "
                 f"{_name_of(hist, ts[j])} pillars")
    return hist.spot * np.exp(out), f"the sheet quotes no forward at {key}; the carry was {where}"


def _name_of(hist: PairHistory, years: float) -> str:
    for k in hist.forwards:
        if abs(tenor_to_years(k) - years) < 1e-12:
            return k
    return f"{years:.3f}y"


def nearest_quoted_tenor(quoted, tenor: str) -> str | None:
    """The quoted tenor closest to ``tenor`` in log time, or ``None``.

    Log time rather than years: 1W is as far from 2W as 6M is from 1Y, and a
    linear distance would answer 2Y for a 1Y request over a 6M one.
    """
    keys = [k for k in quoted if tenor_to_years(k) > 0]
    if not keys:
        return None
    target = math.log(tenor_to_years(tenor))
    return min(keys, key=lambda k: abs(math.log(tenor_to_years(k)) - target))



def volatility_time(pair: str, start: datetime, end: datetime, *,
                    calendars: CalendarSet | None = None,
                    weighting: TimeWeighting | None = None) -> float:
    """Years of *volatility* time between two instants.

    This is the same measure ``AtmCurve.integrated_variance`` uses -- the
    weighting multiplies the instantaneous volatility, so variance time is the
    integral of the squared weight.  Dividing a realized sum of squares by it
    is what makes realized and implied the same kind of number.
    """
    tw = weighting or TimeWeighting(pair, calendars=calendars or DEFAULT_CALENDARS)
    if not tw.enabled:
        return (end - start).total_seconds() / (DAYS_IN_YEAR * 86400.0)
    start = start.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    hours = int(max((end - start).total_seconds(), 0.0) // 3600)
    if hours <= 0:
        return 0.0
    total = 0.0
    seen: dict[date, None] = {}
    for i in range(hours):
        h = start + timedelta(hours=i)
        w = tw.weight_at_datetime(h)
        total += w * w
        seen[h.date()] = None
    return total / (DAYS_IN_YEAR * 24.0)


@dataclass(frozen=True)
class Realized:
    """Realized volatility and shape over one window."""

    pair: str
    start: date
    end: date
    observations: int
    vol: float                       # on the chosen annualisation
    vol_calendar: float
    vol_count: float
    skew: float
    excess_kurtosis: float
    skew_se: float
    kurtosis_se: float
    vol_time: float                  # years of weighted volatility time
    calendar_years: float
    annualisation: str = "weighted"
    #: Which return series the numbers above were measured on -- ``spot`` or
    #: ``forward`` -- and, when it is the forward, the tenor whose swap points
    #: were used and what they contributed.  See ``realized`` for why an
    #: implied volatility is a volatility of the forward and not of spot.
    basis: str = "spot"
    basis_tenor: str | None = None
    vol_spot: float = float("nan")   # the same window measured on spot alone
    vol_forward: float | None = None
    points_vol: float | None = None  # annualised vol of the swap-point term alone
    points_correlation: float | None = None   # corr(spot return, swap-point term)
    carry_rate: float | None = None  # mean annualised carry over the window
    warnings: tuple[str, ...] = ()

    def scaled_skew(self, t: float) -> float:
        """Daily skew projected onto a ``t``-year horizon, assuming independence.

        Skew of a sum of ``n`` independent draws falls as ``1/sqrt(n)``.  The
        risk-neutral skew read off a smile is the skew of the *whole* return to
        expiry, so comparing it against a daily number is comparing two
        different things by a factor of ten or more.
        """
        n = self._steps(t)
        return self.skew / math.sqrt(n) if n > 0 else 0.0

    def scaled_excess_kurtosis(self, t: float) -> float:
        n = self._steps(t)
        return self.excess_kurtosis / n if n > 0 else 0.0

    def _steps(self, t: float) -> float:
        """How many of this window's observations fit into ``t`` years."""
        if self.vol_time <= 0 or self.observations <= 0 or t <= 0:
            return 0.0
        per_observation = self.vol_time / self.observations
        return t / per_observation if per_observation > 0 else 0.0


def _shape(r: np.ndarray) -> tuple[float, float]:
    """Bias-corrected sample skewness and excess kurtosis (G1 and G2).

    The uncorrected ``m3 / m2**1.5`` is biased towards zero on the sample
    sizes a lookback window actually has -- 60 observations is common and the
    correction is a few percent there, which is small next to the standard
    error but free to get right.
    """
    n = r.size
    if n < 4:
        return float("nan"), float("nan")
    d = r - r.mean()
    m2 = float(np.mean(d ** 2))
    if m2 <= 0:
        return float("nan"), float("nan")
    g1 = float(np.mean(d ** 3)) / m2 ** 1.5
    g2 = float(np.mean(d ** 4)) / (m2 * m2) - 3.0
    G1 = g1 * math.sqrt(n * (n - 1.0)) / (n - 2.0)
    G2 = ((n + 1.0) * g2 + 6.0) * (n - 1.0) / ((n - 2.0) * (n - 3.0))
    return G1, G2


def realized(hist: PairHistory, lookback_days: float, *, end: date | None = None,
             annualisation: str = "weighted", calendars: CalendarSet | None = None,
             min_observations: int = 10, basis: str = "spot",
             basis_tenor: str | None = None) -> Realized:
    """Realized volatility, skewness and excess kurtosis over a lookback window.

    ``basis`` decides *what* was realized, and it matters more than it looks.
    A quoted volatility is the volatility of the **forward** to the expiry
    date, not of spot: that is the thing the option is struck against and the
    thing a delta hedge trades.  For most of G10 the two are within a few
    hundredths of a volatility point, because the swap points barely move.
    They are not the same number anywhere the rate differential is large or
    unstable -- a high-carry pair, a managed pair whose points carry the whole
    of the market's opinion, or a turn-of-year window -- and measuring spot
    there understates what the option actually delivered.

    * ``spot``    -- log returns of the spot column.  The previous behaviour,
      and still the only thing available when the sheet has no points.
    * ``forward`` -- log returns of the forward to a **fixed** expiry, rebuilt
      from the constant-maturity quotes the sheet holds.  Writing the outright
      as ``F = S exp(c tau)`` with ``c`` the annualised carry and ``tau`` the
      remaining life, the step from one row to the next is

          dlog F = dlog S + tau * dc - c * dt

      The first two terms are what moved: spot, and the swap points *moving*.
      The third is the points *decaying* by one day of carry, which is a known
      slide and not a risk -- leaving it in the sum of squares would book the
      carry itself as volatility, which is exactly backwards for the pairs
      this basis exists for.  It is removed and reported as ``carry_rate``.
    * ``auto``    -- ``forward`` when the sheet quotes the tenor, ``spot``
      otherwise, saying which it used.

    ``basis_tenor`` names the swap-point column to use.  There is no default:
    the carry term structure is not flat, so a one-week and a one-year forward
    do not realize the same volatility, and the caller knows which tenor it is
    comparing against.
    """
    if annualisation not in ANNUALISATIONS:
        raise ValueError(f"unknown annualisation {annualisation!r}; expected one of {ANNUALISATIONS}")
    if basis not in RETURN_BASES:
        raise ValueError(f"unknown basis {basis!r}; expected one of {RETURN_BASES}")
    if not hist.spot.size:
        raise HistoryError(f"{hist.pair}: the sheet has no spot column, so nothing was realized")
    i, j = hist.window(lookback_days, end)
    px = hist.spot[i:j]
    dates = hist.dates[i:j]
    warnings: list[str] = []

    # The forward series has to survive the same row filter as spot, so the
    # mask is built once from both.  Filtering them separately would leave the
    # two arrays a different length and silently pair up different days.
    fwd = tau = None
    tenor = basis_tenor.upper() if basis_tenor else None
    if basis in ("forward", "auto"):
        why = None
        if tenor is None:
            why = "no tenor was named, so there is no swap-point column to use"
        else:
            series, note = forward_series(hist, tenor)
            if series is None:
                why = (f"the sheet quotes no forward or swap points at all "
                       f"(it has {', '.join(hist.tenors) or 'none'})")
            else:
                fwd = series[i:j]
                tau = tenor_to_years(tenor)
                if note:
                    warnings.append(note)
        if why is not None:
            if basis == "forward":
                raise HistoryError(f"{hist.pair}: a forward-basis realized volatility needs a "
                                   f"forward series, but {why}")
            warnings.append(f"realized on spot rather than the forward: {why}")

    finite = np.isfinite(px) & (px > 0)
    if fwd is not None:
        finite &= np.isfinite(fwd) & (fwd > 0)
    px, dates = px[finite], [d for d, ok in zip(dates, finite) if ok]
    if fwd is not None:
        fwd = fwd[finite]
    dropped = int((~finite).sum())
    if dropped:
        warnings.append(f"{dropped} row(s) in the window had no usable spot and were skipped")
    if px.size < min_observations + 1:
        raise HistoryError(
            f"{hist.pair}: {px.size} usable spot observation(s) in the last "
            f"{lookback_days:g} days; at least {min_observations + 1} are needed"
        )
    r_spot = np.diff(np.log(px))
    gaps = np.array([(b - a).days for a, b in zip(dates[:-1], dates[1:])], dtype=float)
    if gaps.size and float(np.max(gaps)) > 10.0:
        warnings.append(
            f"the largest gap between observations is {float(np.max(gaps)):.0f} days; "
            f"the series is not daily throughout the window"
        )

    points_term = points_vol = points_corr = carry_rate = None
    r = r_spot
    used_basis = "spot"
    if fwd is not None:
        carry = np.log(fwd / px) / tau              # annualised, continuous
        points_term = tau * np.diff(carry)
        r = r_spot + points_term
        carry_rate = float(np.mean(carry))
        used_basis = "forward"

    start_dt = datetime.combine(dates[0], datetime.min.time()).replace(tzinfo=UTC)
    end_dt = datetime.combine(dates[-1], datetime.min.time()).replace(tzinfo=UTC)
    vt = volatility_time(hist.pair, start_dt, end_dt, calendars=calendars)
    cal_years = (end_dt - start_dt).total_seconds() / (DAYS_IN_YEAR * 86400.0)

    # Zero-mean, which is the market convention: over a lookback of months the
    # drift is far smaller than the noise in estimating it, and subtracting a
    # sample mean adds variance to the estimator rather than removing bias.
    def annualise(series: np.ndarray, denominator: float, what: str) -> float:
        if denominator <= 0:
            raise HistoryError(f"{hist.pair}: {what} over the window is not positive")
        return math.sqrt(float(np.sum(series * series)) / denominator)

    ss = float(np.sum(r * r))
    vol_weighted = annualise(r, vt, "weighted volatility time")
    vol_calendar = annualise(r, cal_years, "calendar time")
    vol_count = math.sqrt(ss / (r.size / BUSINESS_DAYS_PER_YEAR))
    chosen = {"weighted": vol_weighted, "calendar": vol_calendar, "count": vol_count}[annualisation]
    denominator = {"weighted": vt, "calendar": cal_years,
                   "count": r.size / BUSINESS_DAYS_PER_YEAR}[annualisation]
    vol_spot = annualise(r_spot, denominator, "the annualisation window")
    vol_forward = chosen if points_term is not None else None
    if points_term is not None:
        points_vol = annualise(points_term, denominator, "the annualisation window")
        sd_s, sd_p = float(np.std(r_spot)), float(np.std(points_term))
        if sd_s > 0 and sd_p > 0:
            points_corr = float(np.corrcoef(r_spot, points_term)[0, 1])

    skew, exkurt = _shape(r)
    n = r.size
    se_skew = math.sqrt(6.0 * n * (n - 1.0) / ((n - 2.0) * (n + 1.0) * (n + 3.0))) if n > 3 else float("nan")
    se_kurt = 2.0 * se_skew * math.sqrt((n * n - 1.0) / ((n - 3.0) * (n + 5.0))) if n > 5 else float("nan")
    if math.isfinite(se_skew) and abs(skew) < se_skew:
        warnings.append(
            f"the realized skew of {skew:+.3f} is inside one standard error ({se_skew:.3f}) "
            f"of zero on {n} observations; it is not distinguishable from noise"
        )
    return Realized(
        pair=hist.pair, start=dates[0], end=dates[-1], observations=n,
        vol=chosen, vol_calendar=vol_calendar, vol_count=vol_count,
        skew=skew, excess_kurtosis=exkurt, skew_se=se_skew, kurtosis_se=se_kurt,
        vol_time=vt, calendar_years=cal_years, annualisation=annualisation,
        basis=used_basis, basis_tenor=(tenor if used_basis == "forward" else None),
        vol_spot=vol_spot, vol_forward=vol_forward, points_vol=points_vol,
        points_correlation=points_corr, carry_rate=carry_rate,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True)
class SeriesStats:
    """Where today's number sits in its own recent history."""

    n: int
    last: float
    mean: float
    low: float
    high: float
    percentile: float | None = None      # of a supplied current value

    @classmethod
    def of(cls, values: np.ndarray, current: float | None = None) -> "SeriesStats | None":
        v = np.asarray(values, dtype=float)
        v = v[np.isfinite(v)]
        if v.size == 0:
            return None
        pct = None
        if current is not None and np.isfinite(current):
            pct = float(np.mean(v <= current) * 100.0)
        return cls(n=int(v.size), last=float(v[-1]), mean=float(np.mean(v)),
                   low=float(np.min(v)), high=float(np.max(v)), percentile=pct)


def implied_stats(hist: PairHistory, lookback_days: float, field_name: str, tenor: str,
                  *, delta: int = 25, end: date | None = None,
                  current: float | None = None) -> SeriesStats | None:
    """Statistics of a quoted series over the lookback window."""
    series = hist.series(field_name, tenor, delta)
    if series is None:
        return None
    i, j = hist.window(lookback_days, end)
    return SeriesStats.of(series[i:j], current)


#: How long a window :func:`vol_dynamics` is measured over unless the caller
#: says otherwise, and deliberately **not** the realized lookback.  A realized
#: volatility is matched to the tenor because a one-month implied volatility
#: forecasts one month; ``rho`` and ``nu`` are properties of the process
#: rather than of a horizon, and measuring them on a one-month window is
#: twenty-odd paired observations of mostly noise.  Worse, the minimum this
#: measurement needs is *higher* than the one a realized volatility needs, so
#: on a short lookback the level comparison went on working while the shape
#: comparison silently had nothing to say at any tenor -- which is what an
#: at-the-money shape of zero beside four blank wings actually was.  Which
#: at-the-money column is read is still the tenor's own, because SABR has no
#: mean reversion and ``nu`` genuinely differs by the tenor of the series; it
#: is the *length of the window* that is a slow measurement, exactly as
#: ``relvalue.HISTORY_DAYS`` is for how much a volatility moves.
DYNAMICS_DAYS = 250.0


@dataclass(frozen=True)
class VolDynamics:
    """What the volatility itself did: how it moved with spot, and how much.

    These are the two numbers a SABR smile is made of.  ``rho`` is the
    correlation between the spot return and the move in the volatility -- the
    thing a risk reversal is paid for -- and ``nu`` is the volatility of that
    volatility, annualised, which is what a butterfly is paid for.  Measuring
    them from history is the only way to hold a quoted wing up against
    something other than another quote.
    """

    pair: str
    tenor: str
    source: str                      # "quoted" | "rolling"
    observations: int
    rho: float
    nu: float
    rho_se: float
    nu_se: float
    vol_mean: float                  # mean level of the volatility series used
    vol_time: float
    warnings: tuple[str, ...] = ()


def vol_dynamics(hist: PairHistory, lookback_days: float, tenor: str, *,
                 end: date | None = None, calendars: CalendarSet | None = None,
                 min_observations: int = 20, rolling_window: int = 21) -> VolDynamics:
    """Realized spot/volatility correlation and volatility of volatility.

    Under SABR with ``beta = 1`` the at-the-money volatility of any expiry is
    the state variable ``alpha`` itself, whose dynamics are
    ``d alpha / alpha = nu dW`` with ``corr(dW, dZ) = rho``.  So the two
    parameters are directly measurable: regress the log change in the quoted
    at-the-money volatility on the log change in spot and you have ``rho``;
    annualise the log change in volatility and you have ``nu``.  That is what
    this does, on the **quoted** at-the-money series when the sheet has one
    for the tenor, and on a rolling realized volatility when it does not.

    Both are annualised on the model's own volatility time, like every other
    realized figure here, so ``nu`` is comparable with the ``nu`` a marked
    smile implies rather than with a 252-day version of it.

    Two things this is not.  SABR has no mean reversion, so a real volatility
    process -- which does revert -- shows a ``nu`` that falls with the tenor
    of the series it is measured on; the number is reported per tenor and must
    not be blended across them.  And the rolling fallback measures an
    *average* of past volatility, whose changes are damped by the averaging,
    so its ``nu`` is a floor rather than an estimate.  It says so.
    """
    key = tenor.upper()
    if not hist.spot.size:
        raise HistoryError(f"{hist.pair}: the sheet has no spot column, so nothing was realized")
    i, j = hist.window(lookback_days, end)
    px = hist.spot[i:j]
    dates = hist.dates[i:j]
    warnings: list[str] = []

    quoted = hist.atm.get(key)
    if quoted is None:
        # The quoted volatility term structure is not something to interpolate
        # -- the *changes* of a made-up column are not the changes of anything
        # -- so the nearest pillar the sheet really quotes is used instead, and
        # named.
        near = nearest_quoted_tenor(hist.atm, key)
        if near is not None:
            quoted = hist.atm[near]
            warnings.append(
                f"the sheet quotes no at-the-money volatility at {key}; the dynamics were "
                f"measured on its {near} column instead")
            key = near
    source = "quoted"
    if quoted is not None:
        vol = quoted[i:j]
        finite = np.isfinite(px) & (px > 0) & np.isfinite(vol) & (vol > 0)
        px, vol = px[finite], vol[finite]
        dates = [d for d, ok in zip(dates, finite) if ok]
    else:
        source = "rolling"
        warnings.append(
            f"the sheet quotes no at-the-money volatility at {key}, so the dynamics were "
            f"measured on a {rolling_window}-observation rolling realized volatility instead. "
            f"A rolling average moves less than the thing it averages, so this vol-of-vol is a "
            f"floor, not an estimate"
        )
        finite = np.isfinite(px) & (px > 0)
        px = px[finite]
        dates = [d for d, ok in zip(dates, finite) if ok]
        if px.size < rolling_window + min_observations + 1:
            raise HistoryError(
                f"{hist.pair}: {px.size} usable spot observation(s) is not enough for a "
                f"{rolling_window}-observation rolling volatility plus {min_observations} "
                f"changes of it"
            )
        step = np.diff(np.log(px))
        # Trailing root mean square over the window, one value per date from
        # ``rolling_window`` onwards.  Unannualised: only its *log changes*
        # are used, and any constant scale cancels out of those.
        sq = np.concatenate(([0.0], np.cumsum(step * step)))
        rms = np.sqrt((sq[rolling_window:] - sq[:-rolling_window]) / rolling_window)
        keep = rms > 0
        vol = rms[keep]
        # The spot series is realigned to the dates the rolling volatility has.
        px = px[rolling_window:][keep]
        dates = dates[rolling_window:]
        dates = [d for d, ok in zip(dates, keep) if ok]

    if px.size < min_observations + 1:
        raise HistoryError(
            f"{hist.pair}: {px.size} paired spot / volatility observation(s) at {key} in the "
            f"last {lookback_days:g} days; at least {min_observations + 1} are needed"
        )

    x = np.diff(np.log(px))
    y = np.diff(np.log(vol))
    n = x.size
    start_dt = datetime.combine(dates[0], datetime.min.time()).replace(tzinfo=UTC)
    end_dt = datetime.combine(dates[-1], datetime.min.time()).replace(tzinfo=UTC)
    vt = volatility_time(hist.pair, start_dt, end_dt, calendars=calendars)
    if vt <= 0:
        raise HistoryError(f"{hist.pair}: weighted volatility time over the window is not positive")

    sd_x, sd_y = float(np.std(x)), float(np.std(y))
    if sd_x <= 0 or sd_y <= 0:
        raise HistoryError(f"{hist.pair}: spot or volatility did not move at {key} over the window")
    rho = float(np.corrcoef(x, y)[0, 1])
    # Zero-mean, for the same reason the realized volatility is zero-mean: the
    # drift in a volatility series over a lookback is far smaller than the
    # noise in estimating it.
    nu = math.sqrt(float(np.sum(y * y)) / vt)
    rho_se = (1.0 - rho * rho) / math.sqrt(n) if n > 2 else float("nan")
    nu_se = nu / math.sqrt(2.0 * n) if n > 0 else float("nan")
    if math.isfinite(rho_se) and abs(rho) < rho_se:
        warnings.append(
            f"the measured spot/volatility correlation of {rho:+.3f} is inside one standard "
            f"error ({rho_se:.3f}) of zero on {n} observations; it is not distinguishable "
            f"from noise"
        )
    return VolDynamics(pair=hist.pair, tenor=key, source=source, observations=n,
                       rho=rho, nu=nu, rho_se=rho_se, nu_se=nu_se,
                       vol_mean=float(np.mean(vol)), vol_time=vt,
                       warnings=tuple(warnings))
