"""The at-the-money volatility term structure.

This replaces the legacy ``CVol``.  The model is unchanged in spirit -- a
mean-reverting instantaneous volatility backbone, an optional rate-volatility
coupling, a short-end add-on, intraday/holiday weighting and dated event spikes,
all integrated to a term volatility -- but three things are different.

**Integration.**  The legacy code called ``scipy.integrate.quad`` with
``limit=500`` on an integrand that is *discontinuous* at every hour boundary and
kinked at every event.  Adaptive quadrature is the wrong tool for that: it
spends its subdivision budget rediscovering jumps whose locations are known in
advance.  Here the integral is split at those known breakpoints and each smooth
panel gets a fixed Gauss-Legendre rule, evaluated in one vectorised call.

**The backbone cross term.**  The legacy expression
``sqrt(sig^2 + 2 rho sig nu + nu^2 t^2)`` is dimensionally inconsistent: the
first and last terms scale as variance while the middle one does not, so its
contribution was effectively arbitrary in the units chosen.  Reading it as the
variance of ``sigma_t + nu W_t`` gives ``2 rho sigma_t nu t``, which is what is
used here.  Set ``rate_vol`` to zero -- as every currency in the sample
workbook does -- and the two agree exactly.

**The clock.**  Nothing calls ``datetime.utcnow()``.  The valuation instant is
supplied once, so an entire surface is built against a single consistent time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .events import EventSchedule, coerce_entry
from .numerics import ConvergenceError, integrate_piecewise, safe_sqrt
from zoneinfo import ZoneInfo

from .calendars import CalendarSet, DEFAULT_CALENDARS
from .timeutil import Clock, DAYS_IN_YEAR, SECONDS_IN_YEAR, UTC, as_utc


def cut_datetime(day: datetime, cut: str, dst_aware: bool = False) -> datetime:
    """The cut instant on ``day``, in UTC."""
    cut = cut.upper()
    if cut not in CUTS:
        raise ValueError(f"unknown cut {cut!r}; expected one of {sorted(CUTS)}")
    if not dst_aware:
        return day.replace(hour=CUTS[cut], minute=0, second=0, microsecond=0)
    tz, hour = CUT_LOCAL[cut]
    local = datetime(day.year, day.month, day.day, hour, tzinfo=ZoneInfo(tz))
    return local.astimezone(UTC)
from .timeweight import TimeWeighting

# Cut times as fixed UTC hours.  These are inherited from the legacy model and
# are what the tool uses by default, so that marks do not move.
CUTS: dict[str, int] = {"HK": 3, "TK": 6, "LDN": 13, "NY": 14}

# The same cuts by their actual definition -- a local time in a named city.
# Converting these through zoneinfo gives a UTC hour that moves with daylight
# saving, which the fixed table above cannot do.  Two of them also disagree
# with the fixed table outright: the New York cut is 10:00 New York, which is
# 15:00Z in winter and 14:00Z in summer, and the legacy "HK" hour of 03:00Z is
# 11:00 Hong Kong rather than the 15:00 local cut.  Enable with
# AtmCurve(dst_aware_cuts=True) once you are ready for the marks to move.
CUT_LOCAL: dict[str, tuple[str, int]] = {
    "HK": ("Asia/Hong_Kong", 15),
    "TK": ("Asia/Tokyo", 15),
    "LDN": ("Europe/London", 15),
    "NY": ("America/New_York", 10),
}
# The 22:00 UTC roll that defines the start of a quoted volatility day.
DAY_ROLL_HOUR = 22


@dataclass
class BackboneParams:
    """Instantaneous volatility backbone parameters, all in decimals.

    ``initial_vol`` and ``long_term_vol`` are the short and asymptotic levels;
    ``mean_reversion`` sets how fast one becomes the other.  ``short_addon``
    with ``short_decay`` lifts the very front end.  ``rate_vol`` and
    ``rate_corr`` couple volatility to a drifting rate.
    """

    initial_vol: float
    long_term_vol: float
    mean_reversion: float = 5.0
    short_addon: float = 0.0
    short_decay: float = 50.0
    rate_vol: float = 0.0
    rate_corr: float = 0.0

    def validate(self) -> list[str]:
        issues = []
        if self.initial_vol <= 0:
            issues.append(f"initial_vol must be positive, got {self.initial_vol:.6g}")
        if self.long_term_vol <= 0:
            issues.append(f"long_term_vol must be positive, got {self.long_term_vol:.6g}")
        if self.mean_reversion < 0:
            issues.append(f"mean_reversion must not be negative, got {self.mean_reversion:.6g}")
        if self.short_decay < 0:
            issues.append(f"short_decay must not be negative, got {self.short_decay:.6g}")
        if not -1.0 <= self.rate_corr <= 1.0:
            issues.append(f"rate_corr must lie in [-1, 1], got {self.rate_corr:.6g}")
        if self.rate_vol < 0:
            issues.append(f"rate_vol must not be negative, got {self.rate_vol:.6g}")
        return issues


class VolCurve:
    """Interface every ATM curve implements, so cross-pairs can compose them."""

    def backbone_vol(self, t):  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class AtmCurve(VolCurve):
    """ATM term structure for one currency pair."""

    pair: str
    params: BackboneParams
    clock: Clock = field(default_factory=Clock.utcnow)
    weighting: TimeWeighting | None = None
    events: EventSchedule = field(default_factory=EventSchedule)
    tenor_overwrites: dict[str, float] = field(default_factory=dict)
    tenor_points: tuple[str, ...] = ("1w", "2w", "3w", "1m", "2m", "3m", "6m", "9m", "1y")
    use_weighting: bool = True
    use_events: bool = True
    quad_order: int = 5
    # Resolve cut times through their local time zone rather than the fixed
    # UTC hours inherited from the legacy model.  On by default: a cut is
    # defined as a local time in a city, so 10:00 New York is 15:00Z in winter
    # and 14:00Z in summer.  Set False to reproduce the legacy fixed hours.
    dst_aware_cuts: bool = True
    # Where a quoted tenor lands.  A "1M" is not 0.0833 years, it is the
    # option expiring on the 1M expiry date of this pair's own calendar, and
    # the curve has to be read at the same place the option is priced --
    # otherwise the marked 1M volatility is not the volatility a 1M option
    # gets.  ``calendars.expiry_years`` is the one reading of that (§4);
    # ``timeutil.tenor_to_years`` stays what it always was, a sort key.
    calendars: CalendarSet = field(default_factory=lambda: DEFAULT_CALENDARS)

    def __post_init__(self) -> None:
        if self.weighting is None:
            self.weighting = TimeWeighting(self.pair, calendars=self.calendars)
        self._horizon = 0.0
        self._edges = np.zeros(0)
        self._int_cache: dict[tuple[float, float], float] = {}

    # -- backbone ---------------------------------------------------------
    def backbone_vol(self, t):
        """Instantaneous volatility before weighting and events.  Vectorised."""
        t = np.asarray(t, dtype=float)
        p = self.params
        sigma = p.long_term_vol - (p.long_term_vol - p.initial_vol) * np.exp(-p.mean_reversion * t)
        if p.rate_vol != 0.0:
            # Variance of sigma_t + nu W_t: the cross term carries a factor of t,
            # which the legacy expression omitted.
            var = sigma * sigma + 2.0 * p.rate_corr * sigma * p.rate_vol * t + (p.rate_vol * t) ** 2
            sigma = np.sqrt(np.maximum(var, 0.0))
        return sigma + p.short_addon * np.exp(-p.short_decay * t)

    def instantaneous_vol(self, t):
        """Backbone after intraday/holiday weighting and event add-ons."""
        t = np.asarray(t, dtype=float)
        vol = self.backbone_vol(t)
        if self.use_weighting and self.weighting.enabled:
            horizon = float(np.max(t)) if t.size else 0.0
            self._ensure_horizon(horizon)
            vol = vol * self.weighting.weights_on(self.clock, t, self._horizon)
        if self.use_events:
            vol = np.maximum(vol + self.events.addon(t), 0.0)
        return vol

    def instantaneous_variance(self, t):
        v = self.instantaneous_vol(t)
        return v * v

    # -- integration ------------------------------------------------------
    def _ensure_horizon(self, t: float) -> None:
        """Extend the cached hourly grid when a longer expiry is requested."""
        if t <= self._horizon and self._edges.size:
            return
        self._horizon = max(t * 1.25 + 1.0 / 365.0, 1.0 / 365.0)
        self._edges, _ = self.weighting.profile(self.clock, self._horizon)

    def _breakpoints(self, t0: float, t1: float) -> np.ndarray:
        """Hourly edges inside ``[t0, t1]``, plus every event time.

        These are exactly the points where the integrand stops being smooth.
        """
        self._ensure_horizon(t1)
        edges = self._edges[(self._edges > t0) & (self._edges < t1)]
        extra = [t0, t1]
        if self.use_events and self.events._times.size:
            ev = self.events._times
            inside = ev[(ev > t0) & (ev < t1)]
            extra.extend(inside.tolist())
            # The spike starts abruptly, so split immediately after it too.
            after = inside + 1.0 / (365.2425 * 24.0)
            extra.extend(after[after < t1].tolist())
        return np.unique(np.concatenate([edges, np.array(extra, dtype=float)]))

    def integrated_variance(self, t1: float, t0: float = 0.0) -> float:
        """Total variance accumulated over ``[t0, t1]``."""
        if t1 <= t0:
            return 0.0
        key = (round(t0, 12), round(t1, 12))
        cached = self._int_cache.get(key)
        if cached is not None:
            return cached
        value = integrate_piecewise(self.instantaneous_variance, self._breakpoints(t0, t1), self.quad_order)
        self._int_cache[key] = value
        return value

    def integrated_vol(self, t1: float, t0: float = 0.0) -> float:
        """Annualised volatility over ``[t0, t1]``."""
        if t1 <= t0:
            raise ValueError(f"integration window must have positive length, got [{t0!r}, {t1!r}]")
        return safe_sqrt(self.integrated_variance(t1, t0) / (t1 - t0), what="integrated variance")

    # -- term structure ---------------------------------------------------
    def tenor_years(self, tenor: str) -> float:
        """Years from the valuation instant to this tenor's calendar expiry."""
        return self.calendars.expiry_years(self.pair, tenor, self.clock)

    def _neighbour_tenors(self, t: float) -> tuple[str | None, str | None]:
        """Tenor points bracketing ``t``.

        The legacy ``get_neighbor_tenors`` used ``np.argmax(tenors > t)``, which
        returns 0 when *no* tenor exceeds ``t``.  Past the last tenor it
        therefore returned the pair ``(last, first)``, and below the first it
        indexed ``[-1]`` -- both silently wrong.  Out-of-range now returns
        ``None`` and the caller falls back to the raw curve.
        """
        ts = np.array([self.tenor_years(x) for x in self.tenor_points])
        order = np.argsort(ts)
        ts, names = ts[order], [self.tenor_points[i] for i in order]
        if t < ts[0] or t > ts[-1]:
            return (None, None)
        idx = int(np.searchsorted(ts, t, side="left"))
        if idx == 0:
            return (names[0], names[0])
        return (names[idx - 1], names[idx])

    def curve_vol(self, t: float) -> float:
        """Term volatility straight off the curve, ignoring overwrites."""
        if t <= 0:
            return 0.0
        return self.integrated_vol(t, 0.0)

    def term_vol(self, t: float) -> float:
        """Term volatility including any tenor overwrites."""
        if t <= 0:
            return 0.0
        if not self.tenor_overwrites:
            return self.curve_vol(t)
        left, right = self._neighbour_tenors(t)
        keys = {k.lower() for k in self.tenor_overwrites}
        if left is None or not ({left.lower(), right.lower()} & keys):
            return self.curve_vol(t)
        if left == right:
            return self.tenor_overwrites.get(left.lower(), self.curve_vol(t))

        t1, t2 = self.tenor_years(left), self.tenor_years(right)
        v1 = self.tenor_overwrites.get(left.lower(), self.curve_vol(t1))
        v2 = self.tenor_overwrites.get(right.lower(), self.curve_vol(t2))
        var1 = self.integrated_variance(t1)
        var2 = self.integrated_variance(t2)
        var_t = self.integrated_variance(t)
        denom = var2 - var1
        if abs(denom) < 1e-18:
            ratio = 0.0 if t2 == t1 else (t - t1) / (t2 - t1)
        else:
            ratio = (var_t - var1) / denom
        # Interpolate in total variance so the overwritten curve stays
        # arbitrage-consistent between the anchors.
        total = ratio * (v2 * v2 * t2 - v1 * v1 * t1) + v1 * v1 * t1
        return safe_sqrt(total / t, what="interpolated total variance")

    def overwrite_tenor(self, tenor: str, vol: float) -> None:
        self.tenor_overwrites[tenor.lower()] = float(vol)

    def clear_overwrite(self, tenor: str | None = None) -> None:
        if tenor is None:
            self.tenor_overwrites.clear()
        else:
            self.tenor_overwrites.pop(tenor.lower(), None)

    # -- volatility days --------------------------------------------------
    def vol_day_start(self, dt: datetime) -> datetime:
        """Start of the NY-cut volatility day containing ``dt``."""
        dt = as_utc(dt)
        anchor = dt.replace(hour=CUTS["NY"], minute=0, second=0, microsecond=0)
        return anchor if dt.hour >= CUTS["NY"] else anchor - timedelta(days=1)

    def daily_vol(self, when, extra_height: float = 0.0) -> float:
        """Annualised volatility of the NY-cut day containing ``when``.

        ``extra_height`` injects a trial event spike; the event calibration
        uses it to invert a quoted daily bump.
        """
        dt = self.clock.coerce_datetime(when)
        start = self.vol_day_start(dt)
        end = start + timedelta(days=1)
        t0, t1 = self.clock.years_to(start), self.clock.years_to(end)
        if t1 <= t0:
            return 0.0
        if extra_height == 0.0:
            return safe_sqrt(self.integrated_variance(t1, t0) / (t1 - t0), what="daily variance")

        t_event = self.clock.years_to(dt)
        decay = self.events.decay

        def variance(t):
            t = np.asarray(t, dtype=float)
            base = self.instantaneous_vol(t)
            spike = np.where(t >= t_event, extra_height * np.exp(-decay * np.maximum(t - t_event, 0.0)), 0.0)
            v = np.maximum(base + spike, 0.0)
            return v * v

        bp = np.unique(np.concatenate([self._breakpoints(t0, t1), np.array([t_event])]))
        bp = bp[(bp >= t0) & (bp <= t1)]
        total = integrate_piecewise(variance, bp, self.quad_order)
        return safe_sqrt(total / (t1 - t0), what="daily variance")

    def cut_vol(self, when, cut: str = "TK") -> float:
        """Term volatility to a named cut, quoted on the whole-day basis.

        Variance accrues over the real time to the cut, but the quote is
        normalised by whole volatility days -- the convention the legacy
        ``getCutVol`` implemented via ``sqrt(t / t0)``.
        """
        cut = cut.upper()
        if cut not in CUTS:
            raise ValueError(f"unknown cut {cut!r}; expected one of {sorted(CUTS)}")
        dt = cut_datetime(self.clock.coerce_datetime(when), cut, self.dst_aware_cuts)
        day_end = dt.replace(hour=DAY_ROLL_HOUR, minute=0, second=0, microsecond=0)
        day_start = self.clock.now.replace(hour=DAY_ROLL_HOUR, minute=0, second=0, microsecond=0)
        t = self.clock.years_to(dt)
        t0 = (day_end - day_start).total_seconds() / SECONDS_IN_YEAR
        if t <= 0 or t0 <= 1e-9:
            return 0.0
        return self.term_vol(t) * math.sqrt(t / t0)

    def daily_series(self, horizon_years: float, cut: str = "NY") -> dict[str, dict[str, float]]:
        """Per-day volatility and running cumulative volatility.

        Replaces ``refreshDailyCumulativeVols``, which mixed a 365-day year in
        its loop bound with a 365.2425-day year in its normalisation.
        """
        cut = cut.upper()
        if cut not in CUTS:
            raise ValueError(f"unknown cut {cut!r}; expected one of {sorted(CUTS)}")
        end_dt = self.clock.now + timedelta(days=horizon_years * DAYS_IN_YEAR)
        out: dict[str, dict[str, float]] = {}
        cum_var = 0.0
        cursor = self.clock.now
        guard = 0
        while cursor < end_dt and guard < 20000:
            guard += 1
            nxt = cut_datetime(cursor, cut, self.dst_aware_cuts)
            if nxt <= cursor:
                nxt = cut_datetime(cursor + timedelta(days=1), cut, self.dst_aware_cuts)
            t0, t1 = self.clock.years_to(cursor), self.clock.years_to(nxt)
            day_var = self.integrated_variance(t1, t0)
            cum_var += day_var
            label = nxt.strftime("%Y/%m/%d")
            day_vol = safe_sqrt(day_var / (t1 - t0), what="daily variance") if t1 > t0 else 0.0
            close = nxt.replace(hour=DAY_ROLL_HOUR, minute=0, second=0, microsecond=0)
            start = self.clock.now.replace(hour=DAY_ROLL_HOUR, minute=0, second=0, microsecond=0)
            tte = (close - start).total_seconds() / SECONDS_IN_YEAR
            hours = (nxt - cursor).total_seconds() / 3600.0
            out[label] = {
                "daily": day_vol,
                "cumulative": safe_sqrt(cum_var / tte, what="cumulative variance") if tte > 1e-9 else 0.0,
                # The first bucket runs from the valuation instant to the next
                # cut, so it is usually shorter than a day.  Its volatility is
                # annualised over its own span and is therefore not comparable
                # with a full day; flag it rather than let it read as one.
                "hours": hours,
                "partial": hours < 23.5,
                # Zero when the expiry falls on the current quoting day, i.e.
                # there are no whole volatility days to normalise by.
                "cumulative_defined": tte > 1e-9,
            }
            cursor = nxt
        return out

    # -- events -----------------------------------------------------------
    def event_window(self, ev) -> tuple[datetime, datetime]:
        """The window a quoted event bump applies to.

        ``forward24h`` is the 24 hours after the release, which is what a
        trader means by "this event adds two vols" and is independent of where
        the 14:00 UTC volatility-day boundary happens to fall.  ``vol_day``
        reproduces the legacy reading.
        """
        if self.events.window_mode == "vol_day":
            start = self.vol_day_start(ev.when)
            return start, start + timedelta(days=1)
        if self.events.window_mode != "forward24h":
            raise ValueError(
                f"unknown event window mode {self.events.window_mode!r}; "
                f"expected 'forward24h' or 'vol_day'"
            )
        return ev.when, ev.when + timedelta(days=1)

    def _window_vol(self, ev, heights: dict) -> float:
        """Volatility of ``ev``'s window with an explicit schedule of heights."""
        start, end = self.event_window(ev)
        t0, t1 = self.clock.years_to(start), self.clock.years_to(end)
        if t1 <= t0:
            return 0.0
        decay = self.events.decay
        times = np.array([self.clock.years_to(e.when) for e in self.events.events])
        hs = np.array([float(heights.get(id(e), 0.0)) for e in self.events.events])
        active = hs != 0.0
        times, hs = times[active], hs[active]

        def variance(t):
            t = np.asarray(t, dtype=float)
            v = self.backbone_vol(t)
            if self.use_weighting and self.weighting.enabled:
                self._ensure_horizon(float(np.max(t)))
                v = v * self.weighting.weights_on(self.clock, t, self._horizon)
            if times.size:
                dt = t[..., None] - times
                on = dt >= 0.0
                v = v + np.where(on, hs * np.exp(-decay * np.where(on, dt, 0.0)), 0.0).sum(axis=-1)
            v = np.maximum(v, 0.0)
            return v * v

        bp = np.unique(np.concatenate([
            self._breakpoints(t0, t1), times[(times >= t0) & (times <= t1)],
            np.array([t0, t1])]))
        bp = bp[(bp >= t0) & (bp <= t1)]
        return safe_sqrt(integrate_piecewise(variance, bp, self.quad_order) / (t1 - t0),
                         what="event window variance")

    def calibrate_events(self) -> list[str]:
        """Solve every event height against this curve.  Returns any problems."""
        self.events.refresh(self.clock)
        self._int_cache.clear()
        problems = self.events.calibrate(self._window_vol, self.clock)
        problems.extend(self.event_sanity_warnings())
        self._int_cache.clear()
        return problems

    def achieved_bump(self, ev) -> float:
        """The marginal volatility bump this event actually delivers.

        Measured over the event's own window, which is what its quote refers
        to.  Its effect on any particular NY-cut volatility day is generally
        smaller, because the event's 24 hours and the quoting day cover
        different mixes of the intraday weight profile.
        """
        heights = {id(e): (e.height or 0.0) for e in self.events.events}
        without = dict(heights)
        without[id(ev)] = 0.0
        return self._window_vol(ev, heights) - self._window_vol(ev, without)

    def event_sanity_warnings(self) -> list[str]:
        """Flag events whose timing makes the quoted bump mean something odd."""
        out = []
        for ev in self.events.events:
            if not ev.height:
                continue
            name = ev.label or ev.when.strftime("%Y-%m-%d %H:%M")
            if self.weighting.is_closed(ev.when):
                out.append(
                    f"{name} at {ev.when:%a %H:%M}Z falls inside the weekly market closure; "
                    f"the bump is being applied to an almost dead window, which is nearly "
                    f"always a wrong date"
                )
                continue
            closed = self.weighting.calendars.holiday_countries(self.pair, ev.when)
            if closed:
                out.append(
                    f"{name} at {ev.when:%Y-%m-%d} falls on a {'/'.join(closed)} holiday "
                    f"for {self.pair}"
                )
        return out

    def set_params(self, **changes) -> list[str]:
        """Update backbone parameters in place and rebuild everything derived.

        Returns any validation problems; the change is rejected if there are
        any, so a bad edit in the UI cannot leave a half-updated curve.
        """
        candidate = BackboneParams(**{**vars(self.params), **changes})
        issues = candidate.validate()
        if issues:
            return issues
        self.params = candidate
        self.invalidate()
        if self.events.events:
            self.calibrate_events()
        return []

    def event_leakage_warnings(self) -> list[str]:
        """Flag events whose spike mostly lands outside the day it was quoted for.

        The spike decays with a roughly one-hour half-life but the volatility
        day rolls at 14:00 UTC, so a release shortly before the roll is
        calibrated against only a sliver of its own day and needs a much larger
        height to produce the quoted bump.  The legacy model behaved the same
        way with no way to see it.
        """
        out = []
        if self.events.window_mode != "vol_day":
            return out          # forward-24h windows cannot leak by construction
        for ev in self.events.events:
            if not ev.height:
                continue
            day_end = self.vol_day_start(ev.when) + timedelta(days=1)
            hours_left = (day_end - ev.when).total_seconds() / 3600.0
            if hours_left <= 0:
                continue
            inside = 1.0 - math.exp(-self.events.decay * hours_left / (DAYS_IN_YEAR * 24.0))
            if inside < 0.90:
                out.append(
                    f"{ev.label or ev.when.strftime('%Y-%m-%d %H:%M')} is {hours_left:.1f}h "
                    f"before the 14:00 UTC volatility-day roll, so only {inside:.0%} of its "
                    f"spike prices into {day_end:%Y-%m-%d}; the rest lands on the next day"
                )
        return out

    def set_events(self, entries) -> list[str]:
        """Replace the event schedule and re-solve every height.

        An entry is an ``events.EventEntry`` or a ``(when, bump, label)``
        tuple.  Each is resolved for this pair: the bump the curve is
        calibrated to is the two legs' weights superposed plus the pair's
        adjustment, and an entry typed as one number is that number.
        """
        self.events.clear()
        problems = []
        for item in entries:
            entry = coerce_entry(item).resolve(self.pair)
            when = as_utc(entry.when)
            label = entry.label
            if when <= self.clock.now:
                problems.append(
                    f"event {label or when.strftime('%Y-%m-%d %H:%M')} is at or before the "
                    f"valuation time and was skipped"
                )
                continue
            self.events.add(when, entry.bump, label, weights=entry.weights, adjust=entry.adjust)
        self.invalidate()
        problems.extend(self.calibrate_events())
        problems.extend(self.event_leakage_warnings())
        return problems

    def invalidate(self) -> None:
        """Drop cached integrals after a parameter or event change.

        The intraday/holiday weight profile is deliberately *not* dropped.  It
        is a pure function of the pair, the clock and the horizon, none of
        which a backbone or event change can touch, and rebuilding it costs
        around 20ms against 2ms for every integral this method actually
        invalidates.  Re-marking a curve therefore used to spend 95% of its
        time recomputing a weight profile that could not have changed, which
        matters the moment anything re-marks in a loop -- the market-maker fit
        does thousands of these.  A caller that mutates the weighting itself
        (or its calendars) in place owns calling ``weighting.clear_cache()``.
        """
        self._int_cache.clear()
        self._edges = np.zeros(0)
        self._horizon = 0.0

    def tenor_table(self) -> list[tuple[str, float]]:
        return [(tp, self.term_vol(self.tenor_years(tp))) for tp in self.tenor_points]
