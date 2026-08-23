"""Scheduled volatility events and their calibration.

An "event" is a dated volatility bump: the user quotes how much a given day's
volatility should rise (in vol points), and the model must back out the height
of a decaying instantaneous-variance spike that reproduces it.

The legacy implementation had two problems.  ``getEventAddOn`` rescanned the
whole event dictionary on *every* quadrature evaluation -- an O(n) dict
comprehension called hundreds of thousands of times per curve.  And
``getEventHeight`` inverted the bump with ``fsolve``, nesting an unchecked
root find around a numerical integration, once per event.

Here the schedule is stored as sorted arrays and evaluated vectorised, and the
inversion is bracketed (the day's volatility is monotone in the event height,
so a bracket always exists and Brent cannot wander).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .numerics import ConvergenceError, solve_scalar
from .timeutil import Clock, as_utc

# Instantaneous-variance decay rate, per year.  At 5000/yr an event is spent
# within a few hours, which is what confines it to its own volatility day.
DEFAULT_EVENT_DECAY = 5000.0


@dataclass
class Event:
    """A dated volatility bump quoted in decimal volatility."""

    when: datetime
    bump: float
    label: str = ""
    height: float | None = None  # filled in by calibration

    def __post_init__(self) -> None:
        self.when = as_utc(self.when)


@dataclass
class EventSchedule:
    """A set of events, evaluated as a vectorised instantaneous add-on."""

    events: list[Event] = field(default_factory=list)
    decay: float = DEFAULT_EVENT_DECAY
    window_days: float = 1.0
    # How the quoted bump is interpreted.
    #
    # "forward24h" (default): the bump applies to the 24 hours *following the
    #   event*.  This is what a trader means by "FOMC adds two vols", and it is
    #   the only reading that is stable -- the spike always has its whole life
    #   inside its own window, so the solved height does not depend on where
    #   the event happens to fall relative to an arbitrary 14:00 UTC boundary.
    #
    # "vol_day" (legacy): the bump applies to the NY-cut volatility day that
    #   contains the event.  An event shortly before the 14:00 roll then has
    #   only minutes of its own day left, so the height explodes and the
    #   overflow lands on the following day.  Kept for comparison only.
    window_mode: str = "forward24h"

    def __post_init__(self) -> None:
        self._times = np.zeros(0)
        self._heights = np.zeros(0)

    def add(self, when: datetime, bump: float, label: str = "") -> Event:
        ev = Event(when, bump, label)
        self.events.append(ev)
        return ev

    def clear(self) -> None:
        self.events.clear()
        self._times = np.zeros(0)
        self._heights = np.zeros(0)

    def sort(self) -> None:
        self.events.sort(key=lambda e: e.when)

    def refresh(self, clock: Clock) -> None:
        """Rebuild the flat arrays used by the vectorised add-on."""
        self.sort()
        calibrated = [e for e in self.events if e.height is not None]
        self._times = np.array([clock.years_to(e.when) for e in calibrated], dtype=float)
        self._heights = np.array([float(e.height) for e in calibrated], dtype=float)

    def addon(self, t: np.ndarray) -> np.ndarray:
        """Instantaneous volatility add-on at times ``t`` (years from valuation).

        Each event contributes ``height * exp(-decay * (t - t_event))`` from its
        own time until the window closes.  Evaluated as a masked broadcast over
        the (few dozen) events rather than a per-point dictionary scan.
        """
        t = np.asarray(t, dtype=float)
        if self._times.size == 0:
            return np.zeros_like(t)
        window = self.window_days / 365.2425
        dt = t[..., None] - self._times  # (..., n_events)
        active = (dt >= 0.0) & (dt <= window)
        contrib = np.where(active, self._heights * np.exp(-self.decay * np.where(active, dt, 0.0)), 0.0)
        return contrib.sum(axis=-1)

    def bumps_in_window(self, end: datetime, days: float = 1.0) -> list[Event]:
        """Events falling in the ``days``-long window ending at ``end``."""
        start = as_utc(end) - timedelta(days=days)
        return [e for e in self.events if start < e.when <= as_utc(end)]

    def total_height_at(self, when: datetime, days: float = 1.0) -> float:
        return sum(float(e.height or 0.0) for e in self.bumps_in_window(when, days))

    def calibrate(self, window_vol_fn, clock: Clock, *, tol: float = 1e-12,
                  max_sweeps: int = 12) -> list[str]:
        """Solve every event height so each reproduces its quoted bump.

        ``window_vol_fn(event, heights)`` returns the volatility of that
        event's window with the given schedule of heights applied.

        Heights are solved **jointly**, by Gauss-Seidel sweeps.  Solving each
        one alone against an event-free curve -- which is what the legacy code
        and the first version here did -- is wrong whenever two events land
        close together: each is calibrated as if it were the only one, and
        once both are switched on neither delivers its quoted bump any more.
        Two events quoted at +2.00 each produced a +4.01 day rather than
        either +2.00 marginal or a stated total.

        Events more than a few hours apart do not interact (the spike is spent
        within about twelve hours), so in practice this converges in one or two
        sweeps and costs no more than the old independent solve.
        """
        problems: list[str] = []
        self.sort()
        live = [e for e in self.events if e.bump != 0.0]
        for ev in self.events:
            if ev.bump == 0.0:
                ev.height = 0.0
            elif ev.height is None:
                ev.height = abs(ev.bump)

        for sweep in range(max_sweeps):
            moved = 0.0
            for ev in live:
                others = {id(e): (e.height or 0.0) for e in live if e is not ev}

                def residual(h: float, _ev=ev, _o=others) -> float:
                    heights = dict(_o)
                    heights[id(_ev)] = h
                    with_ev = window_vol_fn(_ev, heights)
                    heights[id(_ev)] = 0.0
                    without = window_vol_fn(_ev, heights)
                    return (with_ev - without) - _ev.bump

                name = ev.label or ev.when.strftime("%Y-%m-%d %H:%M")
                span = abs(ev.bump) * 400.0 + 1e-6
                try:
                    new = solve_scalar(residual, ev.height or ev.bump,
                                       bracket=(-abs(ev.bump) * 40.0 - 1e-6, span),
                                       xtol=tol, what=f"event height for {name}")
                except (ConvergenceError, ValueError) as exc:
                    new = 0.0
                    problems.append(
                        f"{name}: could not solve for a {ev.bump * 100:+.3f} vol point "
                        f"bump ({exc})"
                    )
                moved = max(moved, abs(new - (ev.height or 0.0)))
                ev.height = new
            if moved <= 1e-10:
                break
        else:
            if live:
                problems.append(
                    f"event heights did not settle in {max_sweeps} sweeps "
                    f"(largest remaining move {moved:.3g}); events may be too close together"
                )
        self.refresh(clock)
        return problems
