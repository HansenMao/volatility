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
from .listed import (GREEK_FIELDS, UNDERLYINGS, WEIGHTINGS, panel_from_request,
                     positions_from_request)
from . import dtcc
from .archive import Archive, ArchiveError
from .knowledge import KnowledgeBank, KnowledgeError, RULE_INSTRUMENTS, RULE_KINDS, SIZE_BASES
from .marketmaker import (BACKBONE_KNOBS, CROSS_KNOBS, DEFAULT_BACKBONE_FREE,
                          DEFAULT_CROSS_FREE, TARGET_SOURCES, learn_from_panel)
from .marketmaker import panel_from_request as mm_panel_from_request
from .marketmaker import quote_panel_from_request as mm_quote_panel_from_request
from .marketmaker import rules_from_request
from .quotes import FLY_CONVENTIONS
from .quotes import VOL_UNITS as QUOTE_VOL_UNITS
from .surface import PARAM_NAMES
from .analytics import TARGETS, carry_table, fair_value_table, realized_table, triangle_table
from .relvalue import HISTORY_DAYS, SHARED, SIGNALS, WEIGHTS
from .relvalue import panel_from_request as relvalue_panel_from_request
from .banded import BAND_MODES, BandTreatment, band_panel
from .curves import CURVE_FIELDS, CURVE_KINDS, FIELD_LABELS, KIND_LABELS
from .curves import panel_from_request as curve_panel_from_request
from .monitor import DEFAULT_WAS_DATE, DEFAULT_WAS_KIND, MAX_TILES
from .monitor import panel_from_request as monitor_panel_from_request
from .history import ANNUALISATIONS, VOL_UNITS, HistoryError, load_history
from .pricing import PRODUCTS, OptionLeg, price_strip, resolve_legs
from . import remarks, screens, session
from .marking import MIN_INSTANCES as MARK_MIN_INSTANCES
from .marking import SCREEN_VERDICTS as MARK_VERDICTS
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
                 history_path: str | None = None, bank_path: str | None = None,
                 session_path: str | None = None, auto_reload: float = 0.0,
                 archive_path: str | None = None, agent_chats=None, agent_sdr=None,
                 ingest_state_path: str | None = None, dtcc_proxy: str | None = None,
                 journal_path: str | None = None):
        self.path = path
        self.clock = clock or Clock.utcnow()
        self.feed_path = feed_path
        # When the feed file was last written, as of the read that is in the
        # book now.  Comparing it with the file on disk is what tells the
        # pricing screen there is fresher spot to be had, without the screen
        # having to poll the numbers themselves.
        self.feed_mtime: float | None = None
        self._lock = threading.RLock()
        self.book: Book | None = None
        self.load_error: str | None = None
        # -- watching the market feed -------------------------------------
        # The feed is the only file that is re-read on its own.  The workbook
        # and the historical sheet are not: a workbook reload discards every
        # mark this session has made (nothing writes to the workbook), and a
        # historical sheet is a record of what happened rather than a market,
        # so neither has any business changing underneath a screen being read.
        # The feed does -- it is a publication, it is republished all morning,
        # and picking it up is the whole point.  Off unless asked for, and the
        # pricing screen has the switch.
        self.auto_interval = (float(auto_reload) if auto_reload and auto_reload > 0
                              else self.AUTO_DEFAULT_INTERVAL)
        self.auto_enabled = bool(auto_reload and auto_reload > 0)
        self.workbook_mtime: float | None = None
        self.history_mtime: float | None = None
        self.auto_events: list[dict] = []
        # Bumped once per reload the watcher performs.  The page polls it
        # rather than the numbers: one integer says "what you are looking at
        # was built from an older file".
        self.auto_seq = 0
        # Whether this session has marked anything the workbook does not
        # hold.  A reload throws those away (that is what a reload is), which
        # is the reason the workbook is not watched at all; it is still
        # reported, so the screen can say the marks are only in memory.
        self.dirty = False
        # A changed file whose write time has not settled yet: read on the
        # pass after the one that first saw it move.  See ``auto_check``.
        self._auto_pending: dict[str, float] = {}
        self._watcher: threading.Thread | None = None
        self._watch_stop = threading.Event()
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
        # The session file: the marks this tool made, kept beside the workbook
        # rather than in it.  Held only as a default path -- the screens post
        # the one they are showing.
        self.session_path = str(session_path) if session_path else None
        self.session_error: str | None = None
        try:
            self.bank = KnowledgeBank.load(bank_path)
        except KnowledgeError as exc:
            self.bank = KnowledgeBank(path=bank_path)
            self.bank_error = str(exc)
        # The observation archive is the desk's own file too, and it has the
        # same life as the bank: a corrupt line in it must not stop the marks
        # loading, so it is read here and its trouble is reported on the card
        # rather than raised at startup.
        self.archive_path = archive_path
        self.archive_error: str | None = None
        # The folders the agent card may scan.  Declared at startup and never
        # taken from the browser: a path posted by a page is a path anything
        # that can reach the page may read, and this server binds to
        # localhost by choice rather than by protocol.
        self.agent_chats = [str(x) for x in (agent_chats or [])]
        self.agent_sdr = [str(x) for x in (agent_sdr or [])]
        self.ingest_state_path = ingest_state_path
        # The proxy the download goes through, from the command line or from
        # the environment.  Not from the browser: a page that can name a proxy
        # can send this server's requests wherever it likes.
        self.dtcc_proxy = dtcc_proxy or dtcc.default_proxy()
        # The archive gets a lock of its own rather than sharing the book's.
        # Reading a folder can take a minute -- a large dissemination file, or
        # a language model working through prose the grammar refused -- and
        # under the book's lock that minute is a minute in which the pricing
        # screen does not answer. Nothing here touches the book beyond
        # borrowing its clock, which is read under the book's lock and let go.
        self._archive_lock = threading.RLock()
        try:
            self.archive = Archive.load(archive_path)
        except ArchiveError as exc:
            self.archive = Archive(path=str(archive_path or ""))
            self.archive_error = str(exc)
        # The re-marking journal, the marking agent's file.  Same life as the
        # archive: the desk's own, read here, trouble reported on the card.
        self.journal_path = journal_path
        self.journal_error: str | None = None
        try:
            self.journal = remarks.Journal.load(journal_path)
        except remarks.RemarkError as exc:
            self.journal = remarks.Journal(path=str(journal_path or ""))
            self.journal_error = str(exc)
        self.reload()
        if session_path:
            # Asked for by name, so a failure is said out loud rather than
            # leaving the book quietly on the workbook's own marks.
            try:
                self.session_load({"path": session_path})
            except Exception as exc:  # noqa: BLE001 - surfaced in /api/state
                self.session_error = f"{type(exc).__name__}: {exc}"
        if history_path:
            try:
                self.load_history({"path": history_path})
            except Exception as exc:  # noqa: BLE001 - surfaced in /api/state
                self.history_error = f"{type(exc).__name__}: {exc}"

    def reload(self) -> dict:
        with self._lock:
            # Read before the load, not after: a workbook saved *while* it was
            # being read would otherwise be stamped with the time of the copy
            # the tool never saw, and the watcher would never pick it up.
            stamp = self._mtime(self.path)
            try:
                self.book = Book.from_excel(self.path, self.clock).load_all()
                if self.feed_path:
                    self.book.feed = MarketFeed.load(self.feed_path)
                    self.feed_mtime = self._feed_mtime()
                self.load_error = None
            except Exception as exc:  # noqa: BLE001 - reported to the browser
                self.load_error = f"{type(exc).__name__}: {exc}"
            self.workbook_mtime = stamp
            # Whatever this session had marked is gone with the old book, so
            # there is nothing left to protect from the next reload.
            self.dirty = False
            return self.state()

    def state(self) -> dict:
        with self._lock:
            if self.book is None:
                return {"pairs": [], "error": self.load_error, "warnings": [],
                        "notes": [], "auto": self.auto_state(),
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
                # Watching the data files belongs to no screen: the workbook,
                # the feed and the historical sheet are read by several.
                "auto": self.auto_state(),
                # A data source rather than one screen's: the analysis and
                # monitor tabs both read it, and either may load it.
                "history": self.history_state(),
                "cuts": sorted(CUTS),
                "analysis": {
                    "targets": [{"key": k, "label": v} for k, v in TARGETS.items()],
                    "annualisations": list(ANNUALISATIONS),
                    "vol_units": list(VOL_UNITS),
                    "curve_kinds": [{"key": k, "label": KIND_LABELS[k]} for k in CURVE_KINDS],
                    "curve_fields": [{"key": f, "label": FIELD_LABELS[f]} for f in CURVE_FIELDS],
                    # The relative-value grid's own vocabulary.  The weights
                    # are a marking judgement rather than a result, so the
                    # page shows them and can send its own back; declared
                    # once, in relvalue.py, so the panel cannot offer a
                    # signal the scorer has never heard of.
                    "signals": [{"key": k, "label": v, "weight": WEIGHTS[k],
                                 "shared": k in SHARED} for k, v in SIGNALS],
                    "history_days": HISTORY_DAYS,
                },
                # The monitor screen shares the curve vocabulary but not the
                # paste source: a tile is a difference between two curves the
                # book can rebuild, and a pasted one cannot be rebuilt on the
                # next refresh.
                "monitor": {
                    "kinds": [{"key": k, "label": KIND_LABELS[k]}
                              for k in CURVE_KINDS if k != "paste"],
                    "fields": [{"key": f, "label": FIELD_LABELS[f]} for f in CURVE_FIELDS],
                    "was_kind": DEFAULT_WAS_KIND, "was_date": DEFAULT_WAS_DATE,
                    "max_tiles": MAX_TILES,
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
                    # The marking agent's file, so the card can say what it
                    # will learn from before anything has been run.
                    "journal": {
                        "path": self.journal.path,
                        "instances": len(self.journal),
                        "pairs": self.journal.pairs(),
                        "problems": list(self.journal.problems),
                        "error": self.journal_error,
                        "verdicts": list(MARK_VERDICTS),
                        "min_instances": MARK_MIN_INSTANCES,
                    },
                },
                "listed": {
                    "underlyings": [
                        {"code": u.code, "name": u.name, "pair": u.pair,
                         "invert": u.invert, "scale": u.scale, "note": u.note}
                        for u in UNDERLYINGS.values()
                    ],
                    "weightings": list(WEIGHTINGS),
                    # The greek columns and what each is measured in, declared
                    # once in listed.py so a column cannot reach the screen
                    # without its unit.
                    "greeks": [{"key": k, "unit": u} for k, u in GREEK_FIELDS],
                },
                "session": {**self.session_state(), "error": self.session_error},
                "valuation": self.book.clock.now.isoformat(),
                "source": str(self.path),
                "warnings": self.book.all_problems(),
                # What the reader worked out rather than read -- a cross
                # broken into its dollar legs, a leg added because a cross
                # needed it.  Not problems, and never silent either.
                "notes": list(self.book.data.notes),
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
            pair = q["pair"]
            surface = self.book[pair]
            method, cut = q.get("method", "SVI"), q.get("cut", "TK")
            sl = surface.slice_at(q["expiry"], method, cut)
            lo = float(min(sl.strikes)) * 0.97
            hi = float(max(sl.strikes)) * 1.03
            ks = np.linspace(lo, hi, 241)
            vols = np.asarray(sl.vol(ks), dtype=float)
            dens = [surface.density(float(k), q["expiry"], method, cut) for k in ks]
            # The smile itself is always built in moneyness -- that is the
            # space the surface works in, and no number here moves because a
            # feed was loaded.  This is only the scale the chart puts on the
            # axis, so a strike reads as the level a trader would name rather
            # than as 1.0234 of a forward.
            level = self.book.market_level(pair, sl.t)
            warnings = list(sl.warnings)
            if level["extrapolated"]:
                warnings.append(
                    f"the {sl.t:.4f}-year forward for {pair} is extrapolated beyond the "
                    f"feed's pillars; the strike axis is scaled by it")
            return {
                "t": sl.t,
                "atm": sl.atm_vol * 100,
                # The axis scale, never the model's own space.  ``forward`` is
                # None when there is no feed and the page stays in moneyness.
                "spot": level["spot"],
                "forward": level["forward"],
                "feed": level["feed"],
                # A level composed from the legs is still a level, and it is
                # still not one the feed published.  The pill says which.
                "derived": level["derived"],
                "via": level["via"],
                "curve": [{"k": float(k), "v": float(v) * 100} for k, v in zip(ks, vols)],
                "density": [{"k": float(k), "d": float(d)} for k, d in zip(ks, dens)],
                "points": [
                    {"label": r["label"], "k": r["strike"], "v": r["vol"] * 100}
                    for r in surface.smile_table(q["expiry"], method=method, cut=cut)
                ],
                "warnings": warnings,
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

    @staticmethod
    def _mtime(path: str | None) -> float | None:
        """When a file was last written, or None if it is not there to ask."""
        if not path:
            return None
        try:
            return Path(path).stat().st_mtime
        except OSError:
            return None

    def _feed_mtime(self) -> float | None:
        """When the feed file on disk was last written, or None if there is none."""
        return self._mtime(self.feed_path)

    def feed_state(self, q: dict | None = None) -> dict:
        """What the spot/forward feed holds, and its quote for one pair.

        ``stale`` says the file on disk has been written since it was read.
        The server does not act on that by itself -- a feed that reloaded
        underneath a price somebody was reading would be its own kind of
        silent change -- it reports it, and the pricing screen offers the
        refresh.
        """
        with self._lock:
            feed = self.book.feed
            if feed is None:
                return {"loaded": False, "pairs": [], "source": "", "problems": [],
                        "path": self.feed_path or "", "stale": False, "written": ""}
            on_disk = self._feed_mtime()
            out = {"loaded": True, "source": feed.source, "asof": feed.asof,
                   "problems": feed.problems, "pairs": feed.summary(),
                   "covered": sorted(feed.pairs),
                   # The crosses the file does not quote and does build: a
                   # feed carrying EURUSD and USDJPY carries EURJPY, and a
                   # status line that counted only the rows in the file said
                   # the pair had no feed on the very screens that price it.
                   "derived": sorted(
                       name for name in self.book.data.pairs
                       if name not in feed.pairs
                       and self.book.market_level(name, 1.0)["feed"]),
                   "path": self.feed_path or "",
                   # When the data in the book was written, taken from the
                   # file rather than from a clock: it is a fact about the
                   # feed, and the model's clock may be a valuation in the past.
                   "written": _stamp(self.feed_mtime),
                   "stale": bool(on_disk is not None and self.feed_mtime is not None
                                 and on_disk > self.feed_mtime)}
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
                self.feed_mtime = None
                return self.feed_state()
            self.book.feed = MarketFeed.load(path)
            self.feed_path = path
            self.feed_mtime = self._feed_mtime()
            return self.feed_state()

    def refresh_feed(self, payload: dict | None = None) -> dict:
        """Re-read the feed file, and quote it at each pricing leg's expiry.

        Two things at once because they are one action on the screen: pick up
        the spot and points that have just been published, and say what they
        are for the options actually on the pad.  The legs come with the
        request like every other screen's panel, so this stays a pure function
        of what it is sent plus the book, and ``Fill`` on the pricing grid can
        write the numbers into the rows a user has typed a stale spot into.

        A leg is reported, never silently skipped: one with no feed for its
        pair, or an expiry that will not resolve, keeps its place and carries
        the reason.  ``pricing.resolve_legs`` does the reading, so what the
        Fill button writes into a row and what the pricer reads out of it come
        from one place and one level lookup.
        """
        payload = payload or {}
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            if not self.feed_path:
                raise FeedError("there is no feed file to refresh; load one first")
            # Read into a local first: a feed file that has been half written,
            # or edited into something unreadable, must not leave the screen
            # with no market at all.
            feed = MarketFeed.load(self.feed_path)
            self.book.feed = feed
            self.feed_mtime = self._feed_mtime()

            legs = resolve_legs(self.book, payload.get("legs") or [])
            return {"ok": True, "legs": legs, "feed": self.feed_state()}

    # -- watching the market feed ------------------------------------------
    #
    # Three files are read by this tool and they have three different lives.
    # The workbook is the book of record and this session's marks are *not* in
    # it (nothing writes to the workbook), so re-reading it throws a morning's
    # marking away -- that is what a reload is for, and it is not something to
    # do to somebody in the background.  The historical sheet is a record of
    # what happened; it does not move during a session in any way a screen
    # needs to chase.  The feed is the one that does: it is a publication, it
    # is republished all morning, and a price quoted off a stale spot is
    # wrong.  So the feed is what is watched, and only the feed.  Both of the
    # others stay on their buttons -- ``Reload workbook`` and the history
    # loader -- where somebody has to mean it.
    #
    # Even the feed is off unless asked for, because a number that changes
    # underneath somebody reading it is its own kind of silent change.  The
    # pricing screen carries the switch, ``--auto-reload`` sets it at startup,
    # and every re-read is written down, counted, and shown on the page.

    AUTO_EVENT_LIMIT = 40
    #: Used when the switch is turned on by hand and no interval was given.
    AUTO_DEFAULT_INTERVAL = 15.0

    @property
    def auto_reload(self) -> float:
        """The poll interval in force, or 0 when the watcher is off."""
        return self.auto_interval if self.auto_enabled else 0.0

    def _auto_targets(self) -> list[tuple[str, str | None, float | None]]:
        """What is watched, where it is, and when the loaded copy was written."""
        return [("feed", self.feed_path, self.feed_mtime)]

    def set_auto(self, payload: dict) -> dict:
        """Turn the feed watcher on or off, and set how often it looks.

        The switch lives on the pricing screen because that is where a stale
        spot does damage, but the setting is the server's: one watcher, one
        interval, whatever a browser happens to have open.
        """
        with self._lock:
            if payload.get("interval") not in (None, ""):
                interval = float(payload["interval"])
                if interval <= 0:
                    raise ValueError(
                        f"the auto-load interval must be positive, got {interval!r} seconds")
                self.auto_interval = interval
            if "enabled" in payload:
                self.auto_enabled = bool(payload["enabled"])
            want = self.auto_enabled
            # A changed interval only reaches the loop through a restart, and
            # the join below must not be done holding the lock: the watcher
            # takes it on every pass.
        self.stop_watching()
        if want:
            self.start_watching()
        with self._lock:
            return self.auto_state()

    def auto_state(self) -> dict:
        with self._lock:
            return {
                "enabled": self.auto_enabled,
                "interval": self.auto_interval,
                # There is nothing to watch without a feed file, and a switch
                # that can be turned on and then does nothing is worse than
                # one that is greyed out with the reason.
                "available": bool(self.feed_path),
                # One integer the page can poll: it says "what you are looking
                # at was built from an older file", without the page having to
                # diff the numbers to find out.
                "seq": self.auto_seq,
                "dirty": self.dirty,
                "watching": [{"what": k, "path": str(pth), "written": _stamp(m)}
                             for k, pth, m in self._auto_targets() if pth],
                "events": list(self.auto_events[-12:]),
            }

    def _auto_record(self, what: str, ok: bool, message: str,
                     when: float | None = None) -> dict | None:
        """Write down one thing the watcher did, unless it just said it.

        The same message about the same file, twice running, is the watcher
        looking at an unchanged situation on every tick -- a feed that is
        still half written, one with no book to go on.  Saying it once and staying
        quiet is what keeps the sequence number meaningful: it moves when
        something actually happened.
        """
        last = next((e for e in reversed(self.auto_events) if e["what"] == what), None)
        if last is not None and last["message"] == message and last["ok"] == ok:
            return None
        self.auto_seq += 1
        ev = {"seq": self.auto_seq, "what": what, "ok": bool(ok), "message": message,
              "when": _stamp(when) or datetime.now(UTC).replace(microsecond=0).isoformat()}
        self.auto_events.append(ev)
        del self.auto_events[:-self.AUTO_EVENT_LIMIT]
        return ev

    def auto_check(self, *, settle: bool = True) -> list[dict]:
        """Re-read the feed if it has been written since it was read.

        Returns the events this pass produced, empty when nothing moved.  The
        watcher thread does nothing else at all, so a test drives this
        directly and there is no timing to get right in it.

        ``settle`` waits for a changed file's write time to stop moving before
        reading it -- the same stamp on two passes running.  A feed is written
        in pieces and half a feed is not a market.  It is done
        that way, rather than by asking how long ago the file was written,
        because a file stamped by another machine can be seconds ahead of this
        one's clock, and a wall-clock settle would then hold it back for as
        long as the two disagreed.  It costs one tick.  A check somebody asked
        for by hand does not wait: they know they have saved.
        """
        with self._lock:
            out = []
            for kind, path, seen in self._auto_targets():
                if not path or seen is None:
                    self._auto_pending.pop(kind, None)
                    continue
                stamp = self._mtime(path)
                if stamp is None or stamp <= seen:
                    self._auto_pending.pop(kind, None)
                    continue
                if settle and self._auto_pending.get(kind) != stamp:
                    self._auto_pending[kind] = stamp
                    continue
                self._auto_pending.pop(kind, None)
                ev = self._auto_apply(kind, path, stamp)
                if ev is not None:
                    out.append(ev)
            return out

    def _auto_apply(self, kind: str, path: str, stamp: float) -> dict | None:
        """Re-read one changed file, and say what came of it.

        A failure does *not* advance the remembered write time, so a file
        caught half written is tried again on the next pass; it is the repeat
        message that is suppressed, never the retry.
        """
        if kind == "feed":
            if self.book is None:
                # Nothing to put it on.  The workbook is the thing that has to
                # be fixed, and it has already said why.
                return self._auto_record("feed", False,
                                         f"{Path(path).name} changed, but no workbook is "
                                         f"loaded to put it on", stamp)
            try:
                feed = MarketFeed.load(path)
            except (FeedError, OSError) as exc:
                return self._auto_record("feed", False,
                                         f"{Path(path).name} changed and could not be read: "
                                         f"{exc}", stamp)
            # Into a local first, then onto the book: a feed half written is
            # not allowed to leave the screen with no market at all.
            self.book.feed = feed
            self.feed_mtime = stamp
            return self._auto_record("feed", True,
                                     f"{Path(path).name} changed and was re-read"
                                     + (f" ({len(feed.problems)} problem(s) in it)"
                                        if feed.problems else ""), stamp)
        return self._auto_record(kind, False, f"nothing knows how to reload {kind!r}", stamp)

    def start_watching(self) -> bool:
        """Start the polling thread.  False when it is off or already running."""
        with self._lock:
            if self.auto_reload <= 0 or self._watcher is not None:
                return False
            self._watch_stop.clear()
            self._watcher = threading.Thread(target=self._watch_loop,
                                             name="volkit-watch", daemon=True)
            self._watcher.start()
            return True

    def stop_watching(self) -> None:
        self._watch_stop.set()
        with self._lock:
            thread, self._watcher = self._watcher, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _watch_loop(self) -> None:
        while not self._watch_stop.wait(self.auto_interval):
            try:
                self.auto_check()
            except Exception as exc:  # noqa: BLE001
                # The watcher has to outlive whatever it trips over, but one
                # that died quietly would be exactly the silent failure this
                # project exists to remove: it says so and keeps going.
                with self._lock:
                    self._auto_record("watch", False, f"{type(exc).__name__}: {exc}")

    def legs(self, payload: dict) -> dict:
        """Resolve the pricing legs' expiry and market boxes, without pricing.

        The screen calls this while somebody is still typing -- a tenor or a
        typed date turns into the one standard date, and the feed's spot and
        outright forward at that expiry go into the two boxes beside it.  The
        feed file is deliberately *not* re-read here: that is what ``Refresh``
        and the auto-load watcher are for, and going to disk on a keystroke
        would make a pause in typing a file read.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            rows = (payload or {}).get("legs") or []
            return {"ok": True, "legs": resolve_legs(self.book, rows),
                    "feed": self.feed_state()}

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
                forward=float(row["forward"]) if row.get("forward") not in (None, "") else None,
                # Points are the other spelling of the same thing and are read
                # only when they are actually sent: a leg that says nothing
                # about them wants the feed's forward, and one that says zero
                # wants the forward at spot.  Defaulting them to 0.0 here made
                # every leg the second kind.
                forward_points=(float(row["points"])
                                if row.get("points") not in (None, "") else None),
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
            # One conversion into the units the panel types in, shared with
            # the session file, so a saved curve and a displayed one cannot
            # come to mean different things.
            out["params"] = session.curve_params(atm)
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
            problems = session.set_curve_params(atm, vals)
            surface.invalidate()
            self.dirty = True
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
                    dt = parse_datetime(str(when))
                except ValueError as exc:
                    raise ValueError(f"event {i + 1}: {exc}") from None
                try:
                    bump = float(row.get("bump") or 0.0) / 100.0
                except (TypeError, ValueError):
                    raise ValueError(f"event {i + 1}: bump must be a number") from None
                entries.append((dt, bump, str(row.get("label") or "")))
            problems = surface.atm.set_events(entries)
            surface.invalidate()
            self.dirty = True
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
            self.dirty = True
            return {"ok": True}

    # -- the session file -------------------------------------------------
    def session_state(self, q: dict | None = None) -> dict:
        """Where the session file is, and whether there is one there.

        The path is *not* server state that the screens have to agree on: the
        browser sends the one it is showing with every save and load, and this
        only supplies the default and reports what is on disk.
        """
        with self._lock:
            path = Path((q or {}).get("path") or self.session_path or session.default_path())
            out = {"path": str(path), "default": str(session.default_path()),
                   "exists": path.exists(), "saved": "", "note": "", "pairs": [],
                   "workbook": "", "problems": []}
            if path.exists():
                try:
                    doc = session.load(path)
                except session.SessionError as exc:
                    out["problems"].append(str(exc))
                    return out
                out["saved"] = str(doc.get("saved") or "")
                out["note"] = str(doc.get("note") or "")
                out["workbook"] = str(doc.get("workbook") or "")
                out["pairs"] = sorted(doc.get("pairs") or {})
            return out

    def session_save(self, payload: dict) -> dict:
        """Write the marks this session has made beside the workbook.

        The workbook itself is untouched -- that is a standing decision, not an
        omission -- so this is how a morning's marking survives a restart.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            raw = (payload.get("pairs") or None)
            pairs = [raw] if isinstance(raw, str) and raw.strip() else raw
            path = (payload.get("path") or "").strip() or (
                self.session_path or str(session.default_path()))
            written = session.save(self.book, path, pairs, note=str(payload.get("note") or ""))
            self.session_path = written
            # The marks are on disk now, so a reload no longer loses them --
            # unless it was one pair of many, which is why the whole book
            # having been saved is what clears it.
            if not pairs:
                self.dirty = False
            return {"ok": True, "written": written,
                    "pairs": (pairs if pairs else self.book.pairs),
                    **self.session_state({"path": written})}

    def session_load(self, payload: dict) -> dict:
        """Put a saved session back on the loaded book.

        Nothing is raised for a pair that will not take its marks: the report
        names it, and the rest of the book still gets what was saved for it.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            raw = (payload.get("pairs") or None)
            pairs = [raw] if isinstance(raw, str) and raw.strip() else raw
            path = (payload.get("path") or "").strip() or (
                self.session_path or str(session.default_path()))
            out = session.restore(self.book, path, pairs)
            self.session_path = str(path)
            # The book now says something the workbook does not.  It is in a
            # file, but a reload would still drop it, so the watcher asks.
            self.dirty = True
            return {"ok": not out["problems"], **out, **self.session_state({"path": path})}

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
            # Stamped before the read and kept whether it worked or not: a
            # file that cannot be read is retried when it changes, not on
            # every pass of the watcher.
            stamp = self._mtime(path)
            try:
                self.history = load_history(path, known,
                                            vol_unit=(payload or {}).get("vol_unit") or "auto")
                self.history_path = path
                self.history_error = None
            except HistoryError as exc:
                self.history = None
                self.history_path = path
                self.history_error = str(exc)
                raise
            finally:
                self.history_mtime = stamp
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
            # What "realized" is measured on.  ``auto`` uses the forward to
            # each tenor wherever the sheet quotes the swap points, because an
            # implied volatility is a volatility of the forward.
            realized_basis = q.get("realized_basis") or "auto"
            with_sabr = str(q.get("sabr", "0")).lower() not in ("0", "false", "no", "")
            sabr_delta = float(q.get("sabr_delta") or 0.25)

            out = {
                "pair": pair, "cut": cut, "method": method, "target": target,
                "target_label": TARGETS.get(target, target),
                "horizon_days": horizon,
                "lookback_days": lookback, "annualisation": annualisation,
                "realized_basis": realized_basis, "sabr": with_sabr, "sabr_delta": sabr_delta,
                "valuation": self.book.clock.now.isoformat(),
                "tenors": list(self.book.data.tenor_points),
                "is_cross": bool(self.book.data.pairs[pair].is_cross),
                "legs": list(self.book.data.pairs[pair].legs),
                # The same lookup every level on every screen comes from, so
                # a cross the feed builds out of its legs is not reported here
                # as having no feed while the carry table prices off one.
                "has_feed": bool(self.book.market_level(pair, 1.0)["feed"]),
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
                lookback_days=lookback, method=method, cut=cut, annualisation=annualisation,
                realized_basis=realized_basis)]
            if hist is not None:
                out["realized"] = [asdict(r) for r in realized_table(
                    self.book, pair, hist, lookback_days=lookback, method=method,
                    cut=cut, annualisation=annualisation, realized_basis=realized_basis,
                    with_sabr=with_sabr, sabr_delta=sabr_delta)]

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

    def relative_value(self, q: dict) -> dict:
        """Score the expiry / strike surface of one pair for relative value.

        Its own route rather than another section of ``/api/analysis``,
        because it is the most expensive thing on the screen -- five passes of
        the rolldown and, for a cross, the whole distribution triangle -- and
        the tables above it must not wait for it.  A pure function of the
        request plus the book, like every other endpoint: the panel is the
        browser's and is posted whole.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            panel = relvalue_panel_from_request(q)
            if panel.pair not in self.book:
                raise ValueError(f"{panel.pair} is not built in this book")
            out = asdict(panel.run(self.book, self.history))
            if self.history is None:
                out["unavailable"]["history"] = (
                    "no historical workbook is loaded, so there is no realized volatility "
                    "to compare against and no scale to score on")
            elif panel.pair not in self.history:
                out["unavailable"]["history"] = (
                    f"the historical workbook has no sheet for {panel.pair}; it holds "
                    f"{', '.join(sorted(self.history.pairs))}")
            out["valuation"] = self.book.clock.now.isoformat()
            return out

    def compare_curves(self, payload: dict) -> dict:
        """Several curves side by side, and the same curve on other dates.

        The panel is the browser's, posted whole, so ``volkit monitor
        --compare`` reproduces the screen exactly and this stays a pure
        function of its request plus the book.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            return curve_panel_from_request(payload).run(self.book, self.history)

    def monitor(self, payload: dict) -> dict:
        """Every tile on the monitor screen: what has moved, and by how much.

        Posted whole like the other panels, so ``volkit monitor`` reproduces
        the screen exactly.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            return monitor_panel_from_request(payload).run(self.book, self.history)

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
            out["has_feed"] = bool(self.book.market_level(pair, 1.0)["feed"])
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
            self.dirty = True
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
        """Fit and fine tune one market-maker panel.  It quotes nothing.

        Like the listed screen, the server keeps no panel state: the browser
        owns the panel and posts it whole, so the same call reproduces the
        same screen from the command line.  What it hands back includes
        ``marks`` -- the parameters the fit arrived at -- which the browser
        holds and posts to :meth:`mm_quote`.  That is what lets a price stand
        on the morning's fit without the server remembering anything.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            panel = mm_panel_from_request(payload)
            out = panel.run(self.book)
            # Only when it left its marks on the book: a panel that reported
            # and restored has changed nothing to lose.
            self.dirty = self.dirty or bool(out.get("applied"))
            return out

    def mm_quote(self, payload: dict) -> dict:
        """Price the instruments in the request box.  It fits nothing.

        The other half of the split.  The marks it stands on come in the
        payload, from a fit the browser is holding; without them it quotes the
        surface as it stands and says so.  The knowledge bank is the one thing
        that *is* server state, because it is a file the desk keeps.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            panel = mm_quote_panel_from_request(payload)
            hist = None
            if self.history is not None and panel.pair in self.history:
                hist = self.history[panel.pair]
            # The archive is the quoting agent's file and the quote's third rung
            # (§17): read under its own lock, like the agent card reads it.
            with self._archive_lock:
                out = panel.run(self.book, bank=self.bank, hist=hist, archive=self.archive)
            out["bank"]["error"] = self.bank_error
            out["archive"]["error"] = self.archive_error
            return out

    def mm_mark(self, payload: dict) -> dict:
        """The marking-agent card: plan the fit on the screen, run it, judge it.

        Aimed at the fit panel's own inputs -- the same paste, the same target
        curve -- so the answer is about the fit the button beside it would
        run.  Nothing stays on the book: the proposal is numbers, the browser
        holds them, and :meth:`mm_mark_record` is the only thing that writes.
        """
        from .marking import panel_from_request as mark_panel_from_request
        panel = mark_panel_from_request(payload)
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            with self._archive_lock:
                out = panel.run(self.book, self.journal, archive=self.archive)
            self.dirty = self.dirty  # a proposal leaves the book as it found it
        out["journal_error"] = self.journal_error
        out["archive_error"] = self.archive_error
        return out

    def mm_mark_record(self, payload: dict) -> dict:
        """What the desk did with a proposal, into the journal.

        The one route on this card that writes -- to the journal, never to the
        workbook.  With ``apply`` the recorded marks also go on the loaded
        book, which is the fit panel's *keep the marks* decision made here.
        """
        from .marking import answer_from_request
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            out = answer_from_request(self.journal, self.book, payload, clock=self.book.clock)
            self.dirty = self.dirty or bool(out.get("applied"))
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

    def mm_agent(self, payload: dict) -> dict:
        """The quoting-agent card: the pasted run's widths against the archive.

        Deliberately does no fitting.  A width comparison needs the paste, the
        bank and the archive and no surface at all, so this answers without
        touching the curve, the wings or the marks -- which is what lets the
        card sit on its own button beside a fit that takes a second.
        """
        from .agent import panel_from_request as agent_panel_from_request
        panel = agent_panel_from_request(payload)
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            book, bank = self.book, self.bank
        with self._archive_lock:
            out = panel.run(book, self.archive, bank=bank)
        out["folders"] = {"chats": list(self.agent_chats), "sdr": list(self.agent_sdr)}
        out["archive"]["error"] = self.archive_error
        return out

    def mm_agent_ingest(self, payload: dict) -> dict:
        """Read whatever is new in the folders this server was started with.

        The folders come from the command line and never from the payload.
        The browser chooses *when*, not *where*: a page that could name a path
        to read is a page that can read anything the server can.
        """
        from . import ingest as ingest_mod
        folders = ([(p, "chat") for p in self.agent_chats]
                   + [(p, "sdr") for p in self.agent_sdr])
        if not folders:
            return {"available": False, "added": 0, "files": [], "notes": [],
                    "reason": ("this server was started with no folders to read; "
                               "restart it with --chats and/or --sdr naming some, "
                               "or fill the archive with 'volkit agent ingest'")}
        with self._lock:
            known = self.book.pairs if self.book is not None else None
        with self._archive_lock:
            state = ingest_mod.State.load(self.ingest_state_path)
            model, model_note = self._agent_model()
            result = ingest_mod.scan(folders, archive=self.archive, state=state,
                                     model=model, known_pairs=known,
                                     force=bool(payload.get("force")))
            written = self.archive.flush()
            state.save()
            return {
                "available": True, "added": result.added, "written": written,
                "unchanged": result.unchanged, "seconds": result.seconds,
                "model": model_note, "summary": result.summary(),
                "notes": list(result.notes) + list(state.problems),
                "files": [{"name": Path(f.path).name, "line": f.line(),
                           "error": f.error, "pairs": f.pairs,
                           "notes": f.notes[:4], "skipped": f.skipped[:6]}
                          for f in result.files],
            }

    #: The most days the page may ask for in one go.  A backfill of a year is
    #: a command-line job with somebody watching it, not a button that can be
    #: leaned on: 250 requests to a public service because a click repeated is
    #: how a desk gets itself blocked.
    FETCH_MAX_DAYS = 30

    def mm_agent_fetch(self, payload: dict) -> dict:
        """Download the dissemination files this desk has not got yet.

        The folder and the proxy come from the command line; the page chooses
        only *how many days back*, and that is capped. What arrives is read
        straight away, because a file downloaded and not ingested is a file
        the desk has to remember to come back for.
        """
        from . import ingest as ingest_mod
        if not self.agent_sdr:
            return {"available": False, "written": 0, "days": [],
                    "reason": ("this server was started with no SDR folder; restart it with "
                               "--sdr DIR to say where the files should go")}
        # ``or 1`` would turn an explicit 0 into a 1, which is a setting the
        # page sent and the server quietly changed.
        raw = payload.get("days", 1)
        raw = 1 if raw in (None, "") else raw
        try:
            days_back = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"days must be a whole number, not {raw!r}")
        if days_back < 1 or days_back > self.FETCH_MAX_DAYS:
            raise ValueError(
                f"ask for between 1 and {self.FETCH_MAX_DAYS} days from the screen; a longer "
                f"backfill is 'volkit agent fetch --since ...', where it can be watched")
        with self._lock:
            today = (self.book.clock.now if self.book is not None
                     else Clock.utcnow().now).date()
            known = self.book.pairs if self.book is not None else None
        folder = self.agent_sdr[0]
        down = dtcc.Downloader(proxy=self.dtcc_proxy)
        with self._archive_lock:
            try:
                result = down.fetch(dtcc.recent_days(days_back, today=today), folder,
                                    today=today)
            except dtcc.DtccError as exc:
                return {"available": True, "written": 0, "days": [], "reason": str(exc),
                        "proxy": self.dtcc_proxy or "", "folder": folder}
            out = {
                "available": True, "written": result.written, "folder": folder,
                "proxy": self.dtcc_proxy or "", "seconds": result.seconds,
                "summary": result.summary(), "reason": "",
                "days": [{"day": d.day.isoformat(), "status": d.status, "line": d.line(),
                          "bytes": d.bytes} for d in result.days],
                "notes": list(result.notes), "read": None,
            }
            if result.written:
                state = ingest_mod.State.load(self.ingest_state_path)
                scan = ingest_mod.scan([(folder, "sdr")], archive=self.archive, state=state,
                                       known_pairs=known)
                self.archive.flush()
                state.save()
                out["read"] = {"added": scan.added, "summary": scan.summary(),
                               "files": [f.line() for f in scan.files]}
            return out

    def _agent_model(self):
        """The local model, or None and the sentence explaining why not.

        Built per request rather than held: a desk starts Ollama halfway
        through the morning, and a server that decided at startup that there
        was no model would go on saying so until it was restarted.
        """
        from .llm import LlmError, LocalModel, ModelConfig
        try:
            model = LocalModel(ModelConfig.from_env())
        except LlmError as exc:
            return None, f"no model: {exc}"
        if not model.available():
            return None, f"no model: {model.why_not}"
        return model, f"model: {model.config.describe()}"

    def mm_ask(self, payload: dict) -> dict:
        """The third agent's card: a question, answered from what is held.

        Reads the archive, the journal, the bank and the surface and writes to
        none of them -- ``ask.AskPanel`` has no route to a writing call, and
        this method adds none.  The transcript arrives with the request and
        goes back with the answer's place in it; the server keeps no turn.
        A workbook is optional: a question about the archive is answered on
        a server whose book failed to load, and one about the surface says
        that the surface is not there.
        """
        from .ask import panel_from_request as ask_panel_from_request
        panel = ask_panel_from_request(payload)
        model, model_note = self._agent_model()
        with self._lock:
            book, bank, hist = self.book, self.bank, self.history
        clock = book.clock if book is not None else None
        with self._archive_lock:
            out = panel.run(book, self.archive, journal=self.journal, bank=bank, hist=hist,
                            model=model, clock=clock)
        out["model_note"] = model_note
        out["archive_error"] = self.archive_error
        out["journal_error"] = self.journal_error
        if book is None:
            out["notes"] = list(out.get("notes") or []) + [
                f"no workbook is loaded ({self.load_error or 'unknown reason'}); the "
                f"surface cannot be asked about"]
        return out

    def mm_agent_file(self, payload: dict) -> dict:
        """Put the run on the screen into the archive.

        What you are quoting against is evidence about how wide this thing is
        shown, and it is evidence that is on the screen already; the only
        thing standing between it and the archive is somebody pressing this.
        """
        from .agent import file_paste
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            clock = self.book.clock
        with self._archive_lock:
            return file_paste(self.archive, payload, clock=clock,
                              counterparty=str(payload.get("counterparty") or "").strip())

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

    def listed_greeks(self, payload: dict) -> dict:
        """Aggregate the risk of a book of exchange-traded positions.

        The browser posts the positions *and* the panels they are priced
        against, so this is a pure function of the request plus the book's
        clock -- the same rule as the fit above, and what lets ``volkit listed
        --positions`` reproduce the screen.  Every parameter the greeks need
        comes off the panel the position belongs to; nothing is looked up on
        the marked surface, because a listed position is risk in the listed
        contract's own units.
        """
        with self._lock:
            if self.book is None:
                raise ValueError(self.load_error or "no workbook is loaded")
            return positions_from_request(payload).run(clock=self.book.clock)

    def export_daily(self, q: dict) -> str:
        with self._lock:
            surface = self.book[q["pair"]]
            series = surface.atm.daily_series(float(q.get("horizon", 1.0)), q.get("cut", "NY"))
            field = q.get("field", "cumulative")
            return "".join(f"{k}, {v[field] * 100}\n" for k, v in series.items())


def _stamp(mtime: float | None) -> str:
    """A file's write time as an ISO string, or empty when there is none.

    Taken from the file rather than from a clock: it is a fact about the file,
    and the model's own clock may be a valuation in the past.
    """
    if not mtime:
        return ""
    return datetime.fromtimestamp(mtime, UTC).replace(microsecond=0).isoformat()


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
            elif url.path == "/api/auto":
                # Belongs to no screen: it is about the files every screen
                # reads.  ``check`` runs a pass now rather than waiting for
                # the next tick, which is what the CLI and the tests use.
                if q.get("check"):
                    self.service.auto_check(settle=False)
                self._json(self.service.auto_state())
            elif url.path == "/api/events/suggest":
                self._json(self.service.suggest_events(q))
            elif url.path == "/api/analysis":
                self._json(self.service.analysis(q))
            elif url.path == "/api/relvalue":
                self._json(self.service.relative_value(q))
            elif url.path == "/api/history":
                self._json(self.service.history_state())
            elif url.path == "/api/session":
                self._json(self.service.session_state(q))
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
            elif url.path == "/api/legs":
                self._json(self.service.legs(payload))
            elif url.path == "/api/curve":
                self._json(self.service.set_curve(payload))
            elif url.path == "/api/events":
                self._json(self.service.set_events(payload))
            elif url.path == "/api/feed":
                self._json(self.service.load_feed(payload))
            elif url.path == "/api/feed/refresh":
                self._json(self.service.refresh_feed(payload))
            elif url.path == "/api/listed/fit":
                self._json(self.service.listed_fit(payload))
            elif url.path == "/api/listed/greeks":
                self._json(self.service.listed_greeks(payload))
            elif url.path == "/api/mm/fit":
                self._json(self.service.mm_fit(payload))
            elif url.path == "/api/mm/quote":
                self._json(self.service.mm_quote(payload))
            elif url.path == "/api/mm/learn":
                self._json(self.service.mm_learn(payload))
            elif url.path == "/api/mm/bank":
                self._json(self.service.mm_save_bank(payload))
            elif url.path == "/api/mm/agent":
                self._json(self.service.mm_agent(payload))
            elif url.path == "/api/mm/agent/ingest":
                self._json(self.service.mm_agent_ingest(payload))
            elif url.path == "/api/mm/agent/file":
                self._json(self.service.mm_agent_file(payload))
            elif url.path == "/api/mm/agent/fetch":
                self._json(self.service.mm_agent_fetch(payload))
            elif url.path == "/api/mm/mark":
                self._json(self.service.mm_mark(payload))
            elif url.path == "/api/mm/ask":
                self._json(self.service.mm_ask(payload))
            elif url.path == "/api/mm/mark/record":
                self._json(self.service.mm_mark_record(payload))
            elif url.path == "/api/analysis":
                self._json(self.service.analysis(payload))
            elif url.path == "/api/relvalue":
                self._json(self.service.relative_value(payload))
            elif url.path == "/api/history":
                self._json(self.service.load_history(payload))
            elif url.path == "/api/session/save":
                self._json(self.service.session_save(payload))
            elif url.path == "/api/session/load":
                self._json(self.service.session_load(payload))
            elif url.path == "/api/monitor/curves":
                self._json(self.service.compare_curves(payload))
            elif url.path == "/api/monitor":
                self._json(self.service.monitor(payload))
            elif url.path == "/api/band":
                self._json(self.service.set_band(payload))
            elif url.path == "/api/auto":
                # Belongs to no screen, like the GET: the feed is read by
                # several of them.  The switch happens to live on the pricing
                # tab because that is where a stale spot does damage.
                self._json(self.service.set_auto(payload))
            else:
                self._json({"error": f"unknown endpoint {url.path}"}, code=404)
        except Exception as exc:  # noqa: BLE001
            self._error(exc)


def serve(path: str, host: str = "127.0.0.1", port: int = 8765,
          clock: Clock | None = None, open_browser: bool = True,
          feed_path: str | None = None, history_path: str | None = None,
          bank_path: str | None = None, session_path: str | None = None,
          auto_reload: float = 0.0, archive_path: str | None = None,
          agent_chats=None, agent_sdr=None, ingest_state_path: str | None = None,
          dtcc_proxy: str | None = None, journal_path: str | None = None) -> None:
    """Start the local server (blocking)."""
    Handler.service = BookService(path, clock, feed_path, history_path, bank_path,
                                  session_path, auto_reload, archive_path,
                                  agent_chats, agent_sdr, ingest_state_path, dtcc_proxy,
                                  journal_path)
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
    if Handler.service.session_error:
        print(f"  ! session: {Handler.service.session_error}")
    if Handler.service.archive_error:
        print(f"  ! observation archive: {Handler.service.archive_error}")
    if Handler.service.journal_error:
        print(f"  ! re-marking journal: {Handler.service.journal_error}")
    watched_folders = Handler.service.agent_chats + Handler.service.agent_sdr
    if watched_folders:
        print(f"  quoting agent watching: {', '.join(watched_folders)}")
    if Handler.service.start_watching():
        watched = ", ".join(w["path"] for w in Handler.service.auto_state()["watching"])
        print(f"  auto-load the feed every {Handler.service.auto_interval:g}s: "
              f"{watched or '(no feed file)'}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        Handler.service.stop_watching()
        httpd.server_close()
