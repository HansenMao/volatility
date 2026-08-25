"""A local web front end for the volatility book.

The legacy interface was Tkinter with every widget held in a module-level
global, business logic inline in the button callbacks, and a bare ``except:``
around the main calculation that turned any error -- a bad strike, a failed
calibration, a missing tenor -- into a silent ``0.0000`` in the output box.
For a pricing tool that is the worst possible failure mode.

This serves the same functions over HTTP so the surface can actually be *seen*:
smile, term structure and daily-vol charts alongside the calculator, with
every warning surfaced rather than swallowed.  It deliberately uses only the
standard library -- no Flask, no FastAPI -- so it runs offline on a desk
machine with nothing to install.  Calculations run on the server thread pool;
the browser stays responsive while a book reloads.
"""

from __future__ import annotations

import json
import math
import threading
import traceback
import webbrowser
from dataclasses import asdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .atm import CUTS
from .exotics import TOUCH_MODES
from .book import Book
from .cross import CrossAtmCurve
from .feed import FeedError, MarketFeed
from .listed import UNDERLYINGS, WEIGHTINGS, panel_from_request
from .knowledge import KnowledgeBank, KnowledgeError, RULE_INSTRUMENTS, RULE_KINDS, SIZE_BASES
from .marketmaker import (BACKBONE_KNOBS, CROSS_KNOBS, DEFAULT_BACKBONE_FREE,
                          DEFAULT_CROSS_FREE, TARGET_SOURCES, learn_from_panel)
from .marketmaker import panel_from_request as mm_panel_from_request
from .marketmaker import rules_from_request
from .quotes import FLY_CONVENTIONS
from .quotes import VOL_UNITS as QUOTE_VOL_UNITS
from .surface import PARAM_NAMES
from .analytics import TARGETS, carry_table, fair_value_table, realized_table, triangle_table
from .banded import BAND_MODES, BandTreatment, band_panel
from .curves import CURVE_FIELDS, CURVE_KINDS, FIELD_LABELS, KIND_LABELS
from .curves import panel_from_request as curve_panel_from_request
from .history import ANNUALISATIONS, VOL_UNITS, HistoryError, load_history
from .pricing import PRODUCTS, OptionLeg, price_strip
from . import screens
from .smile import INTERPOLATORS
from .timeutil import UTC, Clock, parse_datetime, tenor_to_years

STATIC_DIR = Path(__file__).parent / "web"

# Human labels for curve parameters, used in validation messages.
PARAM_LABELS = {
    "initial_vol": "initial vol", "long_term_vol": "long-term vol",
    "mean_reversion": "mean reversion", "short_addon": "short add-on",
    "short_decay": "short decay", "rate_vol": "rate vol", "rate_corr": "rate corr",
    "corr_initial": "correlation initial", "corr_final": "correlation final",
    "corr_decay": "correlation decay",
}


class BookService:
    """Thread-safe wrapper around a Book, with the request handlers."""

    def __init__(self, path: str, clock: Clock | None = None, feed_path: str | None = None,
                 history_path: str | None = None, bank_path: str | None = None):
        self.path = path
        self.clock = clock or Clock.utcnow()
        self.feed_path = feed_path
        self._lock = threading.RLock()
        self.book: Book | None = None
        self.load_error: str | None = None
        # The historical workbook is optional and is held separately from the
        # book: it is a different file with a different life, and a failure to
        # read it must not stop the marks loading.
        self.history = None
        self.history_path = history_path
        self.history_error: str | None = None
        # The knowledge bank is the desk's own file and has a different life
        # again: a bad rule in it must not stop the marks loading either.
        self.bank_path = bank_path
        self.bank_error: str | None = None
        try:
            self.bank = KnowledgeBank.load(bank_path)
        except KnowledgeError as exc:
            self.bank = KnowledgeBank(path=bank_path)
            self.bank_error = str(exc)
        self.reload()
        if history_path:
            try:
                self.load_history({"path": history_path})
            except Exception as exc:  # noqa: BLE001 - surfaced in /api/state
                self.history_error = f"{type(exc).__name__}: {exc}"

    def reload(self) -> dict:
        with self._lock:
            try:
                self.book = Book.from_excel(self.path, self.clock).load_all()
                if self.feed_path:
                    self.book.feed = MarketFeed.load(self.feed_path)
                self.load_error = None
            except Exception as exc:  # noqa: BLE001 - reported to the browser
                self.load_error = f"{type(exc).__name__}: {exc}"
            return self.state()

    def state(self) -> dict:
        with self._lock:
            if self.book is None:
                return {"pairs": [], "error": self.load_error, "warnings": [],
                        "screens": list(screens.enabled())}
            return {
                # Which tabs this build has.  The page hides the rest, and
                # their routes are refused below rather than 404-ing blankly.
                "screens": list(screens.enabled()),
                "pairs": self.book.pairs,
                "tenors": list(self.book.data.tenor_points),
                "methods": list(INTERPOLATORS),
                "products": list(PRODUCTS),
                "overhedges": list(TOUCH_MODES),
                "feed": self.feed_state(),
                "cuts": sorted(CUTS),
                "analysis": {
                    "targets": [{"key": k, "label": v} for k, v in TARGETS.items()],
                    "annualisations": list(ANNUALISATIONS),
                    "vol_units": list(VOL_UNITS),
                    "history": self.history_state(),
                    "curve_kinds": [{"key": k, "label": KIND_LABELS[k]} for k in CURVE_KINDS],
                    "curve_fields": [{"key": f, "label": FIELD_LABELS[f]} for f in CURVE_FIELDS],
                },
                # Managed / pegged pairs.  The page only shows the band card
                # for a pair that has one, and there is no point offering the
                # BAND method on a free floater.
                "bands": {
                    "pairs": self.book.banded_pairs(),
                    "modes": list(BAND_MODES),
                    "default": BandTreatment().to_request(),
                },
                "marketmaker": {
                    "target_sources": list(TARGET_SOURCES),
                    "backbone_knobs": list(BACKBONE_KNOBS),
                    "cross_knobs": list(CROSS_KNOBS),
                    "default_free": list(DEFAULT_BACKBONE_FREE),
                    "default_cross_free": list(DEFAULT_CROSS_FREE),
                    "smile_params": list(PARAM_NAMES),
                    "fly_conventions": list(FLY_CONVENTIONS),
                    "vol_units": list(QUOTE_VOL_UNITS),
                    "rule_kinds": list(RULE_KINDS),
                    "rule_instruments": list(RULE_INSTRUMENTS),
                    "size_bases": list(SIZE_BASES),
                    "bank": self.bank_state(),
                },
                "listed": {
                    "underlyings": [
                        {"code": u.code, "name": u.name, "pair": u.pair,
                         "invert": u.invert, "scale": u.scale, "note": u.note}
                        for u in UNDERLYINGS.values()
                    ],
                    "weightings": list(WEIGHTINGS),
                },
                "valuation": self.book.clock.now.isoformat(),
                "source": str(self.path),
                "warnings": self.book.all_problems(),
                "error": self.load_error,
                "crosses": {k: list(v.legs) for k, v in self.book.data.pairs.items() if v.is_cross},
            }

    # -- endpoints --------------------------------------------------------
    def calc(self, q: dict) -> dict:
        with self._lock:
            pair = q["pair"]
            surface = self.book[pair]
            expiry = q["expiry"]
            method = q.get("method", "SVI")
            cut = q.get("cut", "TK")
            output = q.get("output", "Strike")
            delta = float(q.get("delta", 25)) / 100.0
            forward = float(q.get("forward") or 1.0)
            if forward <= 0:
                raise ValueError(f"forward must be positive, got {forward}")

            if output == "ATM":
                return {"value": surface.atm_vol(expiry, cut) * 100, "unit": "%"}
            if output == "Daily":
                return {"value": surface.daily_vol(expiry) * 100, "unit": "%"}
            if output == "Strike":
                strike = float(q.get("strike") or forward)
                v = float(surface.vol(strike / forward, expiry, method, cut))
                return {"value": v * 100, "unit": "%",
                        "detail": f"K/F = {strike / forward:.6f}"}
            if output == "Delta":
                is_call = str(q.get("callput", "call")).lower() == "call"
                k, v = surface.delta_strike(expiry, delta, is_call, method, cut)
                return {"value": v * 100, "unit": "%",
                        "detail": f"strike = {k * forward:.6f}  (K/F = {k:.6f})"}
            if output == "RR":
                return {"value": surface.risk_reversal(expiry, delta, method, cut) * 100, "unit": "%"}
            if output == "Fly":
                return {"value": surface.strangle(expiry, delta, method, cut) * 100, "unit": "%"}
            if output == "Density":
                strike = float(q.get("strike") or forward)
                return {"value": surface.density(strike / forward, expiry, method, cut), "unit": ""}
            raise ValueError(f"unknown output {output!r}")

    def smile(self, q: dict) -> dict:
        with self._lock:
            surface = self.book[q["pair"]]
            method, cut = q.get("method", "SVI"), q.get("cut", "TK")
            sl = surface.slice_at(q["expiry"], method, cut)
            lo = float(min(sl.strikes)) * 0.97
            hi = float(max(sl.strikes)) * 1.03
            ks = np.linspace(lo, hi, 241)
            vols = np.asarray(sl.vol(ks), dtype=float)
            dens = [surface.density(float(k), q["expiry"], method, cut) for k in ks]
            return {
                "t": sl.t,
                "atm": sl.atm_vol * 100,
                "curve": [{"k": float(k), "v": float(v) * 100} for k, v in zip(ks, vols)],
                "density": [{"k": float(k), "d": float(d)} for k, d in zip(ks, dens)],
                "points": [
                    {"label": r["label"], "k": r["strike"], "v": r["vol"] * 100}
                    for r in surface.smile_table(q["expiry"], method=method, cut=cut)
                ],
                "warnings": list(sl.warnings),
                "fit": None if sl.svi is None else {
                    "rmse_vol_pts": sl.svi.rmse * 100,
                    "max_error_vol_pts": sl.svi.max_abs_vol_error * 100,
                    "arbitrage_free": sl.svi.arbitrage_free,
                },
            }

    def term(self, q: dict) -> dict:
        with self._lock:
            surface = self.book[q["pair"]]
            cut = q.get("cut", "TK")
            tenors = list(self.book.data.tenor_points)
            curve = []
            for tp in tenors:
                t = tenor_to_years(tp)
                curve.append({
                    "tenor": tp, "t": t,
                    "vol": surface.atm.term_vol(t) * 100,
                    "cut": surface.atm.cut_vol(self.clock.datetime_from_years(t), cut) * 100,
                })
            fits = [{
                "tenor": f.tenor, "t": f.t, "atm": f.atm_vol * 100,
                "slog25": f.slog25, "slog10": f.slog10,
                "rho25": f.rho25, "rho10": f.rho10, "ok": f.ok, "message": f.message,
            } for f in surface.fits]
            term = {k: {"initial": v.initial, "final": v.final, "decay": v.decay}
                    for k, v in surface.term.items()}
            return {"curve": curve, "fits": fits, "term": term,
                    "events": [{"when": e.when.isoformat(), "bump": e.bump * 100,
                                "height": e.height, "label": e.label}
                               for e in surface.atm.events.events]}

    def daily(self, q: dict) -> dict:
        with self._lock:
            surface = self.book[q["pair"]]
            horizon = float(q.get("horizon", 1.0))
            series = surface.atm.daily_series(horizon, q.get("cut", "NY"))
            return {"series": [{"date": k, "daily": v["daily"] * 100,
                                "cumulative": v["cumulative"] * 100}
                               for k, v in series.items()]}

    def rrfly(self, q: dict) -> dict:
        """Quoted versus smile-implied risk reversals and butterflies.

        The smile is fitted per tenor, then the parameters are given a term
        structure of their own, so what the surface actually returns at a
        quoted tenor is not identical to the quote that went in.  This is the
        table that shows the difference -- the marking check that the legacy
        tool had no way to display.
        """
        with self._lock:
            surface = self.book[q["pair"]]
            method, cut = q.get("method", "SVI"), q.get("cut", "TK")
            marks = {m.tenor.upper(): m for m in (self.book.data.marks.get(q["pair"]) or [])}
            rows = []
            for fit in surface.fits:
                t = fit.t
                expiry = self.book.clock.datetime_from_years(t)
                mark = marks.get(fit.tenor.upper())
                row = {"tenor": fit.tenor, "t": t, "atm": fit.atm_vol * 100.0}
                for d, tag in ((0.25, "25"), (0.10, "10")):
                    try:
                        rr = surface.risk_reversal(expiry, d, method, cut) * 100.0
                        bf = surface.strangle(expiry, d, method, cut) * 100.0
                    except Exception as exc:  # noqa: BLE001 - one bad tenor must not kill the table
                        row[f"err{tag}"] = f"{type(exc).__name__}: {exc}"
                        continue
                    row[f"rr{tag}"] = rr
                    row[f"bf{tag}"] = bf
                    if mark is not None:
                        qr = (mark.rr_25 if d == 0.25 else mark.rr_10) * 100.0
                        qb = (mark.st_25 if d == 0.25 else mark.st_10) * 100.0
                        row[f"rr{tag}_q"] = qr
                        row[f"bf{tag}_q"] = qb
                        row[f"rr{tag}_d"] = rr - qr
                        row[f"bf{tag}_d"] = bf - qb
                rows.append(row)
            return {"pair": q["pair"], "rows": rows, "anchored": surface.anchor_tenors}

    def feed_state(self, q: dict | None = None) -> dict:
        """What the spot/forward feed holds, and its quote for one pair."""
        with self._lock:
            feed = self.book.feed
            if feed is None:
                return {"loaded": False, "pairs": [], "source": "", "problems": []}
            out = {"loaded": True, "source": feed.source, "asof": feed.asof,
                   "problems": feed.problems, "pairs": feed.summary(),
                   "covered": sorted(feed.pairs)}
            if q and q.get("pair") and q.get("t"):
                try:
                    out["quote"] = feed.quote(q["pair"], float(q["t"]))
                except (FeedError, ValueError) as exc:
                    out["quote_error"] = str(exc)
            return out

    def load_feed(self, payload: dict) -> dict:
        """Point the book at a spot/forward feed file."""
        with self._lock:
            path = (payload.get("path") or "").strip()
            if not path:
                self.book.feed = None
                self.feed_path = None
                return self.feed_state()
            self.book.feed = MarketFeed.load(path)
            self.feed_path = path
            return self.feed_state()

    def price(self, payload: dict) -> dict:
        """Price a strip of option legs against the current marks."""
        rows = payload.get("legs") or []
        if not isinstance(rows, list):
            raise ValueError("'legs' must be a list of option specifications")
        legs = []
        for i, row in enumerate(rows):
            if not row.get("pair"):
                raise ValueError(f"leg {i + 1} has no currency pair")
            legs.append(OptionLeg(
                pair=str(row["pair"]),
                expiry=str(row.get("expiry", "")),
                strike=str(row.get("strike", "ATM")),
                option_type=str(row.get("type", "Auto")),
                cut=str(row.get("cut", "TK")),
                method=str(row.get("method", "SVI")),
                spot=float(row["spot"]) if row.get("spot") not in (None, "") else None,
                forward_points=float(row.get("points") or 0.0),
                pip=float(row.get("pip") or 10000.0),
                notional=float(row.get("notional") or 1.0),
                direction=-1.0 if str(row.get("side", "buy")).lower().startswith("s") else 1.0,
                label=str(row.get("label") or ""),
                product=str(row.get("product") or "vanilla"),
                barrier=str(row.get("barrier") or ""),
                ramp_pct=float(row.get("ramp") or 0.0),
                overhedge=str(row.get("overhedge") or "none"),
                buffer_pct=float(row.get("buffer") or 0.0),
                conservative=str(row.get("side", "buy")).lower().startswith("s"),
            ))
        with self._lock:
            return price_strip(self.book, legs)

    def marks(self, q: dict) -> dict:
        """The marking grid: ATM tenors and per-tenor smile parameters."""
        with self._lock:
            surface = self.book[q["pair"]]
            cut = q.get("cut", "TK")
            atm_rows = []
            for tenor in self.book.data.tenor_points:
                t = tenor_to_years(tenor)
                atm_rows.append({
                    "tenor": tenor,
                    "curve": surface.atm.curve_vol(t) * 100,
                    "marked": surface.atm.term_vol(t) * 100,
                    "cut": surface.atm.cut_vol(self.clock.datetime_from_years(t), cut) * 100,
                    "overwrite": surface.atm.tenor_overwrites.get(tenor.lower()),
                })
            smile_rows = []
            for fit in surface.fits:
                row = {"tenor": fit.tenor, "t": fit.t, "atm": fit.atm_vol * 100, "ok": fit.ok}
                for name in ("slog25", "slog10", "rho25", "rho10"):
                    row[name] = getattr(fit, name)
                    ow = surface.param_overwrites.get(name, {})
                    row[name + "_ow"] = ow.get(fit.tenor.upper())
                smile_rows.append(row)
            return {"atm": atm_rows, "smile": smile_rows,
                    "anchor": surface.anchor_tenors,
                    "pair": q["pair"], "cut": cut}

    # -- curve parameters and events --------------------------------------
    def _event_rows(self, atm) -> list[dict]:
        rows = []
        for e in atm.events.events:
            start = atm.vol_day_start(e.when)
            rows.append({
                "when": e.when.strftime("%Y-%m-%dT%H:%M"),
                "bump": e.bump * 100.0,
                "label": e.label,
                "height": e.height,
                # Which volatility day the bump actually prices into.  The day
                # rolls at 14:00 UTC, so a late-afternoon release belongs to
                # the next one -- worth showing rather than letting a marker
                # discover it from a surprising daily vol.
                "vol_day": (start + timedelta(days=1)).strftime("%Y-%m-%d"),
                "rolls_over": e.when.hour >= CUTS["NY"],
            })
        return rows

    def curve(self, q: dict) -> dict:
        """Marking data for the ATM curve itself: parameters and events."""
        with self._lock:
            surface = self.book[q["pair"]]
            atm = surface.atm
            is_cross = isinstance(atm, CrossAtmCurve)
            out = {
                "pair": q["pair"], "is_cross": is_cross,
                "events": self._event_rows(atm),
                "tenors": list(self.book.data.tenor_points),
            }
            if is_cross:
                spec = self.book.data.pairs[q["pair"]]
                out["legs"] = list(spec.legs)
                out["leg_signs"] = list(atm.leg_signs)
                out["params"] = {
                    "corr_initial": atm.correlation.initial,
                    "corr_final": atm.correlation.final,
                    "corr_decay": atm.correlation.decay,
                    "short_addon": atm.params.short_addon * 100.0,
                    "short_decay": atm.params.short_decay,
                }
            else:
                p = atm.params
                out["params"] = {
                    "initial_vol": p.initial_vol * 100.0,
                    "long_term_vol": p.long_term_vol * 100.0,
                    "mean_reversion": p.mean_reversion,
                    "short_addon": p.short_addon * 100.0,
                    "short_decay": p.short_decay,
                    "rate_vol": p.rate_vol * 100.0,
                    "rate_corr": p.rate_corr,
                }
            return out

    def set_curve(self, payload: dict) -> dict:
        """Re-mark the backbone (or a cross's correlation) in place."""
        with self._lock:
            surface = self.book[payload["pair"]]
            atm = surface.atm
            raw = payload.get("params") or {}
            try:
                vals = {k: float(v) for k, v in raw.items() if v not in (None, "")}
            except (TypeError, ValueError) as exc:
                raise ValueError(f"non-numeric curve parameter: {exc}") from None
            # Validate in the units the user typed, so the message quotes the
            # number they entered rather than its decimal form.
            entry_problems = []
            for key in ("initial_vol", "long_term_vol"):
                if key in vals and vals[key] <= 0:
                    entry_problems.append(f"{PARAM_LABELS[key]} must be positive, got {vals[key]:g}")
            for key in ("mean_reversion", "short_decay", "rate_vol", "corr_decay"):
                if key in vals and vals[key] < 0:
                    entry_problems.append(f"{PARAM_LABELS[key]} must not be negative, got {vals[key]:g}")
            for key in ("rate_corr", "corr_initial", "corr_final"):
                if key in vals and not -1.0 <= vals[key] <= 1.0:
                    entry_problems.append(f"{PARAM_LABELS[key]} must lie in [-1, 1], got {vals[key]:g}")
            if entry_problems:
                return {"ok": False, "problems": entry_problems,
                        **self.curve({"pair": payload["pair"]})}
            if isinstance(atm, CrossAtmCurve):
                problems = atm.set_correlation(
                    vals.get("corr_initial", atm.correlation.initial),
                    vals.get("corr_final", atm.correlation.final),
                    vals.get("corr_decay", atm.correlation.decay))
                if not problems:
                    problems = atm.set_params(
                        short_addon=vals.get("short_addon", atm.params.short_addon * 100) / 100.0,
                        short_decay=vals.get("short_decay", atm.params.short_decay))
            else:
                changes = {}
                for key in ("initial_vol", "long_term_vol", "short_addon", "rate_vol"):
                    if key in vals:
                        changes[key] = vals[key] / 100.0
                for key in ("mean_reversion", "short_decay", "rate_corr"):
                    if key in vals:
                        changes[key] = vals[key]
                problems = atm.set_params(**changes)
            surface.invalidate()
            return {"ok": not problems, "problems": problems, **self.curve({"pair": payload["pair"]})}

    def set_events(self, payload: dict) -> dict:
        """Replace the whole event schedule for a pair and re-solve the heights."""
        with self._lock:
            surface = self.book[payload["pair"]]
            entries = []
            for i, row in enumerate(payload.get("events") or []):
                when = row.get("when")
                if not when:
                    raise ValueError(f"event {i + 1} has no date/time")
                try:
                    dt = parse_datetime(str(when).replace("T", " "))
                except ValueError as exc:
                    raise ValueError(f"event {i + 1}: {exc}") from None
                try:
                    bump = float(row.get("bump") or 0.0) / 100.0
                except (TypeError, ValueError):
                    raise ValueError(f"event {i + 1}: bump must be a number") from None
                entries.append((dt, bump, str(row.get("label") or "")))
            problems = surface.atm.set_events(entries)
            surface.invalidate()
            return {"ok": True, "problems": problems,
                    "events": self._event_rows(surface.atm)}

    def suggest_events(self, q: dict) -> dict:
        """Scheduled economic releases for this pair, ready to accept or edit."""
        with self._lock:
            pair = q["pair"]
            horizon = float(q.get("horizon", 1.0))
            start = self.book.clock.now
            end = start + timedelta(days=horizon * 365.2425)
            found = self.book.econ.for_pair(pair, start, end)
            atm = self.book[pair].atm
            rows = []
            for e in found:
                start_day = atm.vol_day_start(e.when)
                rows.append({**e.as_dict(),
                             "when": e.when.strftime("%Y-%m-%dT%H:%M"),
                             "vol_day": (start_day + timedelta(days=1)).strftime("%Y-%m-%d")})
            return {"pair": pair, "events": rows, "source": self.book.econ.source,
                    "note": "dates from the published calendars shipped with volkit; "
                            "verify before relying on them, and edit "
                            "volkit/data/econ_events.csv to extend"}

    def overwrite(self, q: dict) -> dict:
        with self._lock:
            surface = self.book[q["pair"]]
            kind = q.get("kind", "atm")
            if kind == "atm":
                surface.atm.overwrite_tenor(q["tenor"], float(q["value"]) / 100.0)
            elif kind == "smile":
                surface.overwrite_param(q["param"], q["tenor"], float(q["value"]))
            elif kind == "clear":
                surface.atm.clear_overwrite()
                surface.clear_param_overwrites()
            elif kind == "clear_atm":
                surface.atm.clear_overwrite(q.get("tenor") or None)
            elif kind == "clear_smile":
                surface.clear_param_overwrites()
            elif kind == "anchor":
                surface.anchor_tenors = bool(q["value"])
            else:
                raise ValueError(f"unknown overwrite kind {kind!r}")
            surface.invalidate()
            return {"ok": True}

    # -- analysis ---------------------------------------------------------
    def history_state(self) -> dict:
        h = self.history
        return {
            "loaded": h is not None,
            "source": (h.source if h is not None else (self.history_path or "")),
            "error": self.history_error,
            "pairs": (h.summary() if h is not None else []),
            "problems": (list(h.problems) + list(h.skipped_sheets)) if h is not None else [],
        }

    def load_history(self, payload: dict) -> dict:
        with self._lock:
            path = (payload or {}).get("path") or self.history_path
            if not path:
                raise ValueError("no historical workbook path was given")
            known = self.book.pairs if self.book is not None else None
            try:
                self.history = load_history(path, known,
                                            vol_unit=(payload or {}).get("vol_unit") or "auto")
                self.history_path = path
                self.history_error = None
            except HistoryError as exc:
                self.history = None
                self.history_error = str(exc)
                raise
            return self.history_state()

    def analysis(self, q: dict) -> dict:
        """Every section of the analysis screen for one pair.

        Sections are built independently and each carries its own reason for
        being empty, so a missing forward feed does not take the realized
        statistics down with it and a pair that is not a cross simply has no
        triangle.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            pair = q["pair"]
            if pair not in self.book:
                raise ValueError(f"{pair} is not built in this book")
            cut = q.get("cut", "NY")
            method = q.get("method") or None
            target = q.get("target", "atm")
            horizon = float(q.get("horizon_days") or 30.0)
            lookback_raw = q.get("lookback_days")
            lookback = None if lookback_raw in (None, "", "match") else float(lookback_raw)
            annualisation = q.get("annualisation") or "weighted"
            with_noise = str(q.get("noise", "1")).lower() not in ("0", "false", "no")

            out = {
                "pair": pair, "cut": cut, "method": method, "target": target,
                "target_label": TARGETS.get(target, target),
                "horizon_days": horizon,
                "lookback_days": lookback, "annualisation": annualisation,
                "valuation": self.book.clock.now.isoformat(),
                "tenors": list(self.book.data.tenor_points),
                "is_cross": bool(self.book.data.pairs[pair].is_cross),
                "legs": list(self.book.data.pairs[pair].legs),
                "has_feed": bool(self.book.feed is not None
                                 and pair.upper() in getattr(self.book.feed, "pairs", {})),
                "carry": None, "fair": None, "realized": None, "triangle": None,
                "unavailable": {},
            }

            rows = carry_table(self.book, pair, horizon_days=horizon, target=target,
                               method=method, cut=cut)
            out["carry"] = [asdict(r) for r in rows]

            hist = None
            if self.history is not None and pair in self.history:
                hist = self.history[pair]
            elif self.history is None:
                out["unavailable"]["history"] = (
                    "no historical workbook is loaded, so there is nothing to compare the "
                    "marks against")
            else:
                out["unavailable"]["history"] = (
                    f"the historical workbook has no sheet for {pair}; it holds "
                    f"{', '.join(sorted(self.history.pairs))}")

            out["fair"] = [asdict(r) for r in fair_value_table(
                self.book, pair, hist, horizon_days=horizon,
                lookback_days=lookback, method=method, cut=cut, annualisation=annualisation)]
            if hist is not None:
                out["realized"] = [asdict(r) for r in realized_table(
                    self.book, pair, hist, lookback_days=lookback, method=method,
                    cut=cut, annualisation=annualisation)]

            if out["is_cross"]:
                try:
                    out["triangle"] = [asdict(r) for r in triangle_table(
                        self.book, pair, method=method, cut=cut, with_noise=with_noise)]
                except ValueError as exc:
                    out["unavailable"]["triangle"] = str(exc)
            else:
                out["unavailable"]["triangle"] = (
                    f"{pair} is not a cross, so there is no triangle to check")
            return out

    def compare_curves(self, payload: dict) -> dict:
        """Several curves side by side, and the same curve on other dates.

        The panel is the browser's, posted whole, so ``volkit analysis
        --compare`` reproduces the screen exactly and this stays a pure
        function of its request plus the book.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            return curve_panel_from_request(payload).run(self.book, self.history)

    # -- managed bands ----------------------------------------------------
    def band(self, q: dict) -> dict:
        """The band read-out for one pair, under the treatment now marked."""
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            pair = q["pair"]
            if pair not in self.book:
                raise ValueError(f"{pair} is not built in this book")
            surface = self.book[pair]
            tenors = [q["tenor"]] if q.get("tenor") else list(self.book.data.tenor_points)
            out = band_panel(surface, tenors, cut=q.get("cut", "NY"))
            out["has_feed"] = bool(self.book.feed is not None
                                   and pair.upper() in getattr(self.book.feed, "pairs", {}))
            return out

    def set_band(self, payload: dict) -> dict:
        """Re-mark the band treatment for one pair, then report it.

        The treatment lives on the surface beside the parameter shifts, for
        the same reason: it is part of how the pair is marked, not part of a
        screen's state, and every screen that prices this pair must see the
        same one.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            pair = payload["pair"]
            if pair not in self.book:
                raise ValueError(f"{pair} is not built in this book")
            surface = self.book[pair]
            warnings = surface.set_band_treatment(BandTreatment.from_request(payload))
            out = self.band(payload)
            out["warnings"] = list(dict.fromkeys(list(out["warnings"]) + warnings))
            return out

    # -- market maker -----------------------------------------------------
    def bank_state(self, q: dict | None = None) -> dict:
        """What is in the knowledge bank, and where it came from."""
        with self._lock:
            out = {
                "path": self.bank.path,
                "pairs": sorted(self.bank.pairs),
                "problems": list(self.bank.problems),
                "error": self.bank_error,
            }
            pair = (q or {}).get("pair")
            if pair:
                pk = self.bank.for_pair(pair)
                out["pair"] = pair.upper()
                out["rules"] = [asdict(r) for r in pk.rules]
                out["describe"] = [r.describe() for r in pk.rules]
                out["updated"] = pk.updated
                out["source_note"] = pk.source_note
            return out

    def mm_fit(self, payload: dict) -> dict:
        """Fit, fine tune and quote one market-maker panel.

        Like the listed screen, the server keeps no panel state: the browser
        owns the panel and posts it whole, so the same call reproduces the
        same screen from the command line.  The knowledge bank is the one
        thing that *is* server state, because it is a file the desk keeps.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            panel = mm_panel_from_request(payload)
            hist = None
            if self.history is not None and panel.pair in self.history:
                hist = self.history[panel.pair]
            out = panel.run(self.book, bank=self.bank, hist=hist)
            out["bank"]["error"] = self.bank_error
            return out

    def mm_learn(self, payload: dict) -> dict:
        """Propose bank rules from the widths the pasted market showed.

        Proposing and saving are deliberately two steps: a paste that happens
        to contain one wide quote should not be able to rewrite the desk's
        ladder without somebody looking at it.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            rules, notes, parse = learn_from_panel(payload, self.book.clock)
            return {"rules": [asdict(r) for r in rules],
                    "describe": [r.describe() for r in rules],
                    "notes": notes, "parse": parse}

    def mm_save_bank(self, payload: dict) -> dict:
        """Replace one pair's rules and write the file.

        The whole set is posted and validated together; a set with a bad rule
        in it is rejected whole rather than saved half applied, so the file on
        disk is never a state the screen never showed.
        """
        with self._lock:
            pair = str(payload.get("pair") or "").strip().upper()
            if not pair:
                raise ValueError("a currency pair is required to save knowledge against")
            rules = rules_from_request(payload)
            problems = self.bank.set_pair(pair, rules, self.clock.now,
                                          str(payload.get("source_note") or ""))
            if problems:
                return {"ok": False, "problems": problems, **self.bank_state({"pair": pair})}
            written = self.bank.save(self.bank_path)
            self.bank_error = None
            return {"ok": True, "problems": [], "written": written,
                    **self.bank_state({"pair": pair})}

    def listed_fit(self, payload: dict) -> dict:
        """Fit one exchange-traded panel and compare it with the marked surface.

        The server keeps no panel state: the browser owns the list of panels
        and posts a whole one each time, so a fit is a pure function of its
        request plus the loaded book.  That is what makes the same call
        reproducible from the command line.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            return panel_from_request(payload).run(self.book)

    def export_daily(self, q: dict) -> str:
        with self._lock:
            surface = self.book[q["pair"]]
            series = surface.atm.daily_series(float(q.get("horizon", 1.0)), q.get("cut", "NY"))
            field = q.get("field", "cumulative")
            return "".join(f"{k}, {v[field] * 100}\n" for k, v in series.items())


def _finite(obj):
    """Replace every non-finite float with ``None``, recursively."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if isinstance(obj, np.floating):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return [_finite(v) for v in obj.tolist()]
    return obj


class Handler(BaseHTTPRequestHandler):
    service: BookService = None  # injected
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        # NaN and Infinity are what Python's json emits for a non-finite float
        # and what JSON.parse refuses to read, so a single unavailable cell
        # would take a whole response down in the browser.  They become null,
        # which the front end already renders as a dash.
        self._send(code, json.dumps(_finite(payload), default=str).encode(), "application/json")

    def _error(self, exc: Exception) -> None:
        # Surfacing the actual message is the whole point: the legacy UI
        # replaced every failure with a silent zero.
        self._json({"error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(limit=3)}, code=400)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        gone = screens.route_refusal(url.path)
        if gone is not None:
            return self._json({"error": gone}, code=404)
        try:
            if url.path in ("/", "/index.html"):
                self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._json(self.service.state())
            elif url.path == "/api/calc":
                self._json(self.service.calc(q))
            elif url.path == "/api/smile":
                self._json(self.service.smile(q))
            elif url.path == "/api/marks":
                self._json(self.service.marks(q))
            elif url.path == "/api/curve":
                self._json(self.service.curve(q))
            elif url.path == "/api/rrfly":
                self._json(self.service.rrfly(q))
            elif url.path == "/api/feed":
                self._json(self.service.feed_state(q))
            elif url.path == "/api/events/suggest":
                self._json(self.service.suggest_events(q))
            elif url.path == "/api/analysis":
                self._json(self.service.analysis(q))
            elif url.path == "/api/history":
                self._json(self.service.history_state())
            elif url.path == "/api/band":
                self._json(self.service.band(q))
            elif url.path == "/api/mm/bank":
                self._json(self.service.bank_state(q))
            elif url.path == "/api/term":
                self._json(self.service.term(q))
            elif url.path == "/api/daily":
                self._json(self.service.daily(q))
            elif url.path == "/api/export/daily":
                body = self.service.export_daily(q).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{q.get("pair", "vol")}_daily_vol.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json({"error": f"unknown endpoint {url.path}"}, code=404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            return self._error(exc)
        gone = screens.route_refusal(url.path)
        if gone is not None:
            return self._json({"error": gone}, code=404)
        try:
            if url.path == "/api/reload":
                self._json(self.service.reload())
            elif url.path == "/api/overwrite":
                self._json(self.service.overwrite(payload))
            elif url.path == "/api/calc":
                self._json(self.service.calc(payload))
            elif url.path == "/api/price":
                self._json(self.service.price(payload))
            elif url.path == "/api/curve":
                self._json(self.service.set_curve(payload))
            elif url.path == "/api/events":
                self._json(self.service.set_events(payload))
            elif url.path == "/api/feed":
                self._json(self.service.load_feed(payload))
            elif url.path == "/api/listed/fit":
                self._json(self.service.listed_fit(payload))
            elif url.path == "/api/mm/fit":
                self._json(self.service.mm_fit(payload))
            elif url.path == "/api/mm/learn":
                self._json(self.service.mm_learn(payload))
            elif url.path == "/api/mm/bank":
                self._json(self.service.mm_save_bank(payload))
            elif url.path == "/api/analysis":
                self._json(self.service.analysis(payload))
            elif url.path == "/api/history":
                self._json(self.service.load_history(payload))
            elif url.path == "/api/analysis/curves":
                self._json(self.service.compare_curves(payload))
            elif url.path == "/api/band":
                self._json(self.service.set_band(payload))
            else:
                self._json({"error": f"unknown endpoint {url.path}"}, code=404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


def serve(path: str, host: str = "127.0.0.1", port: int = 8765,
          clock: Clock | None = None, open_browser: bool = True,
          feed_path: str | None = None, history_path: str | None = None,
          bank_path: str | None = None) -> None:
    """Start the local server (blocking)."""
    Handler.service = BookService(path, clock, feed_path, history_path, bank_path)
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    print(f"volkit serving {path}\n  -> {url}\n  (Ctrl-C to stop)")
    if screens.summary():
        print(f"  {screens.summary()}")
    if Handler.service.load_error:
        print(f"  ! load error: {Handler.service.load_error}")
    if Handler.service.history_error:
        print(f"  ! history: {Handler.service.history_error}")
    if Handler.service.bank_error:
        print(f"  ! knowledge bank: {Handler.service.bank_error}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
