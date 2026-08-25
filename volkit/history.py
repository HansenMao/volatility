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
from .timeutil import UTC, DAYS_IN_YEAR, TenorError, parse_tenor, tenor_to_years
from .timeweight import TimeWeighting

ANNUALISATIONS = ("weighted", "calendar", "count")
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
        book = pd.ExcelFile(path)
    except Exception as exc:  # noqa: BLE001 - reported with the file name
        raise HistoryError(f"could not open {path}: {type(exc).__name__}: {exc}") from None

    out = History(source=str(path))
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
             min_observations: int = 10) -> Realized:
    """Realized volatility, skewness and excess kurtosis over a lookback window."""
    if annualisation not in ANNUALISATIONS:
        raise ValueError(f"unknown annualisation {annualisation!r}; expected one of {ANNUALISATIONS}")
    if not hist.spot.size:
        raise HistoryError(f"{hist.pair}: the sheet has no spot column, so nothing was realized")
    i, j = hist.window(lookback_days, end)
    px = hist.spot[i:j]
    dates = hist.dates[i:j]
    finite = np.isfinite(px) & (px > 0)
    px, dates = px[finite], [d for d, ok in zip(dates, finite) if ok]
    warnings: list[str] = []
    dropped = int((~finite).sum())
    if dropped:
        warnings.append(f"{dropped} row(s) in the window had no usable spot and were skipped")
    if px.size < min_observations + 1:
        raise HistoryError(
            f"{hist.pair}: {px.size} usable spot observation(s) in the last "
            f"{lookback_days:g} days; at least {min_observations + 1} are needed"
        )
    r = np.diff(np.log(px))
    gaps = np.array([(b - a).days for a, b in zip(dates[:-1], dates[1:])], dtype=float)
    if gaps.size and float(np.max(gaps)) > 10.0:
        warnings.append(
            f"the largest gap between observations is {float(np.max(gaps)):.0f} days; "
            f"the series is not daily throughout the window"
        )

    start_dt = datetime.combine(dates[0], datetime.min.time()).replace(tzinfo=UTC)
    end_dt = datetime.combine(dates[-1], datetime.min.time()).replace(tzinfo=UTC)
    vt = volatility_time(hist.pair, start_dt, end_dt, calendars=calendars)
    cal_years = (end_dt - start_dt).total_seconds() / (DAYS_IN_YEAR * 86400.0)

    # Zero-mean, which is the market convention: over a lookback of months the
    # drift is far smaller than the noise in estimating it, and subtracting a
    # sample mean adds variance to the estimator rather than removing bias.
    ss = float(np.sum(r * r))
    def annualise(denominator: float, what: str) -> float:
        if denominator <= 0:
            raise HistoryError(f"{hist.pair}: {what} over the window is not positive")
        return math.sqrt(ss / denominator)

    vol_weighted = annualise(vt, "weighted volatility time")
    vol_calendar = annualise(cal_years, "calendar time")
    vol_count = math.sqrt(ss / (r.size / BUSINESS_DAYS_PER_YEAR))
    chosen = {"weighted": vol_weighted, "calendar": vol_calendar, "count": vol_count}[annualisation]

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
