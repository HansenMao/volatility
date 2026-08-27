"""Command line interface.

The legacy tool could only be driven through its Tkinter window, so nothing
was scriptable and nothing could run in a batch job.  Every operation the GUI
offered is available here too, plus a ``check`` command that validates a
workbook without building anything.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

# archive, llm and synthesis are imported at module level because
# ``build_parser`` names their constants in the agent command's help and
# choices, and a parser cannot be built from a module imported inside a
# function.  All three are stdlib-only, so this does not drag the numeric
# stack into anything that did not already have it.
from . import (archive, config, consult, dtcc, llm, marking, paths, remarks,
               screens, session, synthesis)
from .book import Book
from .marketdata import ExcelSource, MarketDataError
from .pricing import expiry_datetime
from .timeutil import UTC, Clock, parse_datetime, tenor_to_years

#: Every subcommand name, whether or not this build registers it.  Used before
#: the parser exists, to tell a configuration file's command from its options.
KNOWN_COMMANDS = frozenset(screens.all_commands()) | {"check", "serve", "session"}

def _default_workbook() -> str:
    """The workbook beside the executable, or the project copy when running from source."""
    from .paths import find_data_file
    found = find_data_file("vol_marks.xlsx", "files/vol_marks.xlsx")
    return str(found) if found else "files/vol_marks.xlsx"


def _targets():
    from .analytics import TARGETS
    return TARGETS


def _target_sources():
    from .marketmaker import TARGET_SOURCES
    return TARGET_SOURCES


def _methods():
    from .smile import INTERPOLATORS
    return INTERPOLATORS


def _band_modes():
    from .banded import BAND_MODES
    return BAND_MODES


def _curve_fields():
    from .curves import CURVE_FIELDS
    return CURVE_FIELDS


def _history_days() -> float:
    from .relvalue import HISTORY_DAYS
    return HISTORY_DAYS


def _weights(pairs) -> dict[str, float]:
    """``--weight name=value`` as a dictionary, or the reason it is not one.

    A malformed weight is refused rather than skipped: a run that quietly
    scored on the defaults while the command line said otherwise would be the
    same silent disagreement between what is shown and what is used that the
    panel's own weights are written to avoid.
    """
    out: dict[str, float] = {}
    for item in pairs or []:
        if "=" not in item:
            raise ValueError(f"--weight wants SIGNAL=NUMBER, got {item!r}")
        name, _, value = item.partition("=")
        out[name.strip()] = value.strip()
    return out


def _default_feed() -> str | None:
    from .paths import find_data_file
    found = find_data_file("market_feed.csv", "files/market_feed.csv")
    return str(found) if found else None


def _default_history() -> str | None:
    """The user's own historical workbook, never the shipped sample.

    ``files/history_sample.xlsx`` is synthetic.  Auto-loading it would put
    made-up realized volatility on the analysis screen without anyone asking
    for it, which is the one thing the screen must not do; it is loaded only
    when named explicitly.
    """
    from .paths import find_data_file
    found = find_data_file("vol_history.xlsx", "files/vol_history.xlsx")
    return str(found) if found else None


def _band_treatment(args):
    """The band treatment from the command line, in the screen's own units.

    Percentages in, decimals out, converted exactly once -- the same edge rule
    the pasted quotes and the knowledge bank follow.
    """
    from .banded import BandTreatment
    return BandTreatment.from_request({
        "mode": getattr(args, "band_mode", None) or ("mixture" if getattr(args, "method", "")
                                                     == "BAND" else "warn"),
        "hazard": getattr(args, "hazard", None),
        "weak_share": getattr(args, "weak_share", None),
        "weak_jump": getattr(args, "weak_jump", None),
        "weak_vol": getattr(args, "weak_vol", None),
        "strong_jump": getattr(args, "strong_jump", None),
        "strong_vol": getattr(args, "strong_vol", None),
        "lower": getattr(args, "band_lower", None),
        "upper": getattr(args, "band_upper", None),
        "blend": getattr(args, "band_blend", None),
        "delta": getattr(args, "band_delta", None),
        "solve_hazard": getattr(args, "solve_hazard", False),
    })


def _apply_band(args, book) -> None:
    """Hand the marked treatment and any feed to the surfaces that have a band."""
    if getattr(args, "feed", None) and book.feed is None:
        from .feed import MarketFeed
        try:
            book.feed = MarketFeed.load(args.feed)
        except Exception as exc:  # noqa: BLE001 - the lognormal surface still works
            print(f"  ! forward feed: {exc}", file=sys.stderr)
    treatment = _band_treatment(args)
    for name in book.banded_pairs():
        for w in book[name].set_band_treatment(treatment):
            print(f"  ! {name}: {w}", file=sys.stderr)


def _clock(args) -> Clock:
    if getattr(args, "asof", None):
        return Clock(parse_datetime(args.asof))
    return Clock.utcnow()


def _book(args, pairs=None) -> Book:
    book = Book.from_excel(args.workbook, _clock(args))
    book.load_all(pairs)
    for w in book.all_problems():
        print(f"  ! {w}", file=sys.stderr)
    # A saved session goes on here rather than in each subcommand, so every
    # command prices against the same marks the screen would be showing.  It
    # is applied after load_all so it is layered on the workbook, never
    # instead of it, and everything it could not put back is printed.
    if getattr(args, "session", None):
        from . import session as session_mod
        out = session_mod.restore(book, args.session)
        print(f"session {args.session}: marks restored for "
              f"{', '.join(out['applied']) or 'nothing'}"
              + (f" (saved {out['saved']})" if out["saved"] else ""), file=sys.stderr)
        for note in out["notes"]:
            print(f"  . {note}", file=sys.stderr)
        for problem in out["problems"]:
            print(f"  ! {problem}", file=sys.stderr)
    return book


def cmd_check(args) -> int:
    """Validate a workbook and report everything wrong with it at once."""
    try:
        data = ExcelSource(args.workbook).load()
    except MarketDataError as exc:
        print(f"FAILED: {exc}")
        return 2
    print(f"{data.source}")
    print(f"  pairs   : {len(data.pairs)} "
          f"({sum(1 for p in data.pairs.values() if p.is_cross)} crosses)")
    print(f"  tenors  : {', '.join(data.tenor_points)}")
    print(f"  quotes  : {sum(len(m) for m in data.marks.values())} across {len(data.marks)} sheets")
    print(f"  events  : {sum(len(p.events) for p in data.params.values())}")
    for note in data.notes:
        print(f"    . {note}")
    if data.problems:
        print(f"\n  {len(data.problems)} problem(s):")
        for p in data.problems:
            print(f"    - {p}")
        return 1
    print("\n  no problems found")
    return 0


def cmd_tenors(args) -> int:
    book = _book(args, [args.pair])
    surface = book[args.pair]
    print(f"{args.pair}  (valuation {book.clock.now:%Y-%m-%d %H:%M}Z, cut {args.cut})")
    print(f"  {'tenor':<6}{'curve %':>10}{'cut %':>10}")
    for tenor in book.data.tenor_points:
        t = tenor_to_years(tenor)
        cut = surface.atm.cut_vol(book.clock.datetime_from_years(t), args.cut)
        print(f"  {tenor:<6}{surface.atm.term_vol(t) * 100:>10.4f}{cut * 100:>10.4f}")
    return 0


def cmd_daily(args) -> int:
    book = _book(args, [args.pair])
    series = book[args.pair].atm.daily_series(args.horizon, args.cut)
    lines = [f"{k}, {v[args.field] * 100}" for k, v in series.items()]
    if args.out:
        paths.write_text(args.out, "\n".join(lines) + "\n")
        print(f"wrote {len(lines)} rows to {args.out}")
    else:
        print("\n".join(lines))
    return 0


def cmd_vol(args) -> int:
    book = _book(args, [args.pair])
    _apply_band(args, book)
    surface = book[args.pair]
    # The pricing screen's own expiry box, on the command line: a tenor is
    # resolved on the pair's calendar and a date is read in any of the
    # spellings `timeutil` takes.  The two must not understand different
    # things -- this command is that screen's equivalent.
    expiry = expiry_datetime(book, args.pair, args.expiry)
    if args.strike is None:
        print(f"{surface.atm_vol(expiry, args.cut) * 100:.6f}")
        return 0
    v = surface.vol(args.strike / args.forward, expiry, args.method, args.cut)
    print(f"{float(v) * 100:.6f}")
    return 0


def cmd_smile(args) -> int:
    book = _book(args, [args.pair])
    _apply_band(args, book)
    surface = book[args.pair]
    expiry = expiry_datetime(book, args.pair, args.expiry)
    sl = surface.slice_at(expiry, args.method, args.cut)
    print(f"{args.pair}  expiry {expiry:%Y-%m-%d}  t={sl.t:.5f}y  method={args.method}")
    # The same scale the marking screen's chart uses: with a feed the strikes
    # are printed as levels, without one they stay in K/F and the heading says
    # so.  The smile is fitted in moneyness either way.
    level = book.market_level(args.pair, sl.t)
    scale = level["forward"] if level["feed"] else 1.0
    print(f"  {'point':<10}{'strike' if level['feed'] else 'K/F':>12}{'vol %':>10}")
    for row in surface.smile_table(expiry, method=args.method, cut=args.cut):
        print(f"  {row['label']:<10}{row['strike'] * scale:>12.6f}{row['vol'] * 100:>10.4f}")
    if level["feed"]:
        print(f"  spot {level['spot']:.6f}  forward {scale:.6f}"
              + (f"  (the {level['via']} triangle; the feed does not quote "
                 f"{args.pair.upper()} itself)" if level["derived"] else "")
              + ("  (extrapolated beyond the feed's pillars)"
                 if level["extrapolated"] else ""))
    if sl.svi is not None:
        flag = "arbitrage-free" if sl.svi.arbitrage_free else "ARBITRAGE PRESENT"
        print(f"  fit: max error {sl.svi.max_abs_vol_error * 100:.2e} vol pts, {flag}")
    for w in sl.warnings:
        print(f"  ! {w}")
    return 0


def cmd_band(args) -> int:
    """The managed-band read-out: how much of this smile is peg-break premium.

    The same function the marking screen's band card calls, so a figure quoted
    off the screen can be reproduced in a batch job.
    """
    from .banded import band_panel

    book = _book(args, [args.pair])
    _apply_band(args, book)
    surface = book[args.pair]
    tenors = [args.tenor] if args.tenor else list(book.data.tenor_points)
    panel = band_panel(surface, tenors, cut=args.cut)

    print(f"{args.pair}   valuation {book.clock.now:%Y-%m-%d %H:%M}Z   cut {args.cut}")
    if not panel["has_band"]:
        print(f"  {panel['message']}")
        return 2
    b = panel["band"]
    edges = f"[{b['effective_lower']:g}, {b['effective_upper']:g}]"
    print(f"  band    {edges}"
          + (f"  (policy [{b['lower']:g}, {b['upper']:g}], overridden here)"
             if b["overridden"] else "")
          + (f"\n          {b['note']}" if b["note"] else ""))
    print(f"  marked  {panel['describe']}")
    for w in panel["warnings"]:
        print(f"  ! {w}")
    print(f"\n  {'tenor':<7}{'forward':>10}{'atm %':>9}{'P(break)':>10}{'P(out)':>9}"
          f"{'P(below)':>10}{'P(above)':>10}{'body shift':>12}{'band RR':>9}{'logn RR':>9}"
          f"  shape")
    failed = 0
    for r in panel["rows"]:
        if r["message"]:
            failed += 1
            print(f"  {r['tenor']:<7}  {r['message']}")
            continue
        fwd = "—" if r["forward"] is None else f"{r['forward']:.5f}"
        print(f"  {r['tenor']:<7}{fwd:>10}{r['atm'] * 100:>9.4f}{r['prob_broken'] * 100:>10.4f}"
              f"{r['prob_outside_band'] * 100:>9.4f}{r['prob_below'] * 100:>10.4f}"
              f"{r['prob_above'] * 100:>10.4f}{r['in_band_mean_shift']:>+12.6f}"
              f"{r['model_rr'] * 100:>9.4f}{r['lognormal_rr'] * 100:>9.4f}"
              f"  {'U-shaped' if r['u_shaped'] else 'bell'} a={r['a']:.3f} b={r['b']:.3f}")
    print("\n  Break risk is a marked input, never inferred from a butterfly: a wider body "
          "and a\n  higher hazard both raise the at-the-money, so a joint fit is degenerate. "
          "--solve-hazard\n  inverts it deliberately and reports what it depended on.")
    return 1 if failed else 0


def cmd_analysis(args) -> int:
    """Carry and roll, realized against implied, fair value, and the triangle.

    The same four sections the Analysis screen shows, in the same order and
    from the same functions, so a number quoted off the screen can be
    reproduced in a batch job.
    """
    from . import analytics
    from .feed import MarketFeed
    from .history import load_history

    def pct(v, d=3, w=8):
        """A percentage, or a right-aligned dash of the same width.

        Returning a bare "—" collapses the column and shifts every field after
        it, which is how an unavailable cell turns a readable table into an
        unreadable one.
        """
        if v is None or not isinstance(v, (int, float)) or v != v:
            return "—".rjust(w)
        return f"{v * 100:{w}.{d}f}"

    def sgn(v, d=3, w=8):
        if v is None or not isinstance(v, (int, float)) or v != v:
            return "—".rjust(w)
        return f"{v * 100:+{w}.{d}f}"

    # Book.build expands the request to a cross's legs on its own, so asking for
    # the cross alone still builds everything the triangle needs.
    book = _book(args, [args.pair])
    if args.feed:
        try:
            book.feed = MarketFeed.load(args.feed)
        except Exception as exc:  # noqa: BLE001 - the carry still works without it
            print(f"  ! forward feed: {exc}", file=sys.stderr)
    hist = history = None
    if args.history:
        history = load_history(args.history, book.pairs, vol_unit=args.vol_unit)
        for p in list(history.problems) + list(history.skipped_sheets):
            print(f"  . {p}", file=sys.stderr)
        hist = history[args.pair] if args.pair in history else None
        if hist is None:
            print(f"  ! no sheet for {args.pair} in {args.history}", file=sys.stderr)

    lookback = None if args.lookback in (None, "match") else float(args.lookback)
    print(f"{args.pair}   valuation {book.clock.now:%Y-%m-%d %H:%M}Z   cut {args.cut}   "
          f"{args.method}   horizon {args.horizon:g} days")

    carry = analytics.carry_table(book, args.pair, horizon_days=args.horizon,
                                  target=args.target, method=args.method, cut=args.cut)
    print(f"\ncarry and roll — {analytics.TARGETS[args.target]}")
    print(f"  {'tenor':<6}{'forward':>10}{'strike':>10}{'level':>9}{'rolled':>9}{'roll':>9}"
          f"{'term':>9}{'smile':>9}{'per year':>10}{'/atm':>8}"
          f"{'delta':>9}{'bs delta':>10}{'skew':>8}"
          f"{'fwd/yr':>9}{'carry bp':>10}{'in vols':>9}{'roll+carry':>11}")
    for r in carry:
        if not r.expiry:
            print(f"  {r.tenor:<6}  {r.warnings[0]}")
            continue
        strike = "—" if r.strike != r.strike else f"{r.strike:.5f}"
        # The carry is a premium in the term currency, so it is divided by the
        # forward before being called basis points -- 0.019 JPY on a 150
        # forward is 1.3bp of notional, not 192 of anything. "in vols" is the
        # same premium over the position's own vega, which is the form that
        # can be read beside the roll.
        print(f"  {r.tenor:<6}{r.forward:>10.5f}{strike:>10}{pct(r.level)}{pct(r.level_rolled)}"
              f"{sgn(r.roll)}{sgn(r.roll_term)}{sgn(r.roll_smile)}{sgn(r.roll_annual, 2, 10)}"
              f"{r.ratio_atm:>+8.3f}"
              # The smile delta leads: it is the one the desk hedges with and
              # the one that values the forward move, skew included.
              f"{('—'.rjust(9) if r.smile_delta is None else f'{r.smile_delta:>+9.4f}')}"
              f"{('—'.rjust(10) if r.delta is None else f'{r.delta:>+10.4f}')}"
              f"{('—'.rjust(8) if r.skew_delta is None else f'{r.skew_delta:>+8.4f}')}"
              f"{sgn(r.carry_rate, 2, 9)}"
              f"{('—'.rjust(10) if r.carry_pnl is None else f'{r.carry_pnl / r.forward * 10000:>+10.3f}')}"
              f"{sgn(r.carry_vols, 3, 9)}"
              f"{sgn(None if r.carry_vols is None else r.roll + r.carry_vols, 3, 11)}")
    for note in dict.fromkeys(w for r in carry for w in r.warnings if r.expiry):
        print(f"  ! {note}")
    print("  delta / fwd-yr / carry: the forward curve's own carry. The strike is fixed, so the "
          "forward\n  slides out from under it — that is 'smile' in volatility points and "
          "'carry bp' in basis\n  points of the forward. A book hedged in the outright forward to "
          "the option's own expiry\n  earns and pays that away exactly; hedged in spot, as a desk "
          "is, it keeps it.")
    print("  'delta' is the smile delta: the strike is fixed and the forward slides under it, so "
          "the\n  volatility that strike is marked at moves too, and dV/dF carries "
          "vega*dsigma/dF with it.\n  'bs delta' holds the volatility still — what a "
          "Black-Scholes delta is — and 'skew' is the\n  difference, which is the whole of what "
          "the smile contributes. The at-the-money is a\n  delta-neutral straddle only in the "
          "Black-Scholes column; on a skewed surface its smile delta\n  is not zero, and that is "
          "the number you are running. Both are dV/dF in the term currency, so\n  delta x (F2-F1) "
          "is the carry — not the premium-adjusted quoted delta, which is a hedge\n  ratio in the "
          "other currency and does not turn a move in the forward into money.")

    fair = analytics.fair_value_table(book, args.pair, hist, horizon_days=args.horizon,
                                      lookback_days=lookback, method=args.method, cut=args.cut,
                                      annualisation=args.annualisation,
                                      realized_basis=args.realized_basis)
    print(f"\nfair value — at-the-money implied against realized plus the at-the-money roll and carry")
    print(f"  {'tenor':<6}{'implied':>9}{'realized':>9}{'on':>9}{'window':>8}{'roll':>9}{'x':>7}"
          f"{'roll val':>9}{'of which fwd':>13}{'fwd/yr':>9}{'carry val':>10}{'fair':>9}{'rich':>9}")
    for r in fair:
        print(f"  {r.tenor:<6}{pct(r.implied)}{pct(r.realized)}{r.realized_basis:>9}"
              f"{(r.realized_window_days or 0):>8.0f}{sgn(r.roll)}{r.roll_multiplier:>7.1f}"
              f"{sgn(r.roll_value)}{sgn(r.forward_value, 3, 13)}{sgn(r.carry_rate, 2, 9)}"
              f"{sgn(r.carry_value, 4, 10)}{pct(r.fair)}{sgn(r.richness)}")
    print("  'on' is what the realized number was measured on. An implied volatility is a "
          "volatility of\n  the forward, so where the sheet quotes swap points the forward is "
          "used and their moves count.\n  'carry val' is the forward curve's price-side carry as "
          "a break-even volatility; the at-the-money\n  is a delta-neutral straddle, so it is "
          "second order there and 'of which fwd' carries the first.")

    if hist is not None:
        rows = analytics.realized_table(book, args.pair, hist, lookback_days=lookback,
                                        method=args.method, cut=args.cut,
                                        annualisation=args.annualisation,
                                        realized_basis=args.realized_basis,
                                        with_sabr=args.sabr, sabr_delta=args.sabr_delta)
        print(f"\nrealized against implied  ({args.annualisation} annualisation)")
        print(f"  {'tenor':<6}{'window':>7}{'obs':>5}{'realized':>9}{'on':>9}{'on spot':>9}"
              f"{'pts vol':>9}{'implied':>9}{'premium':>9}"
              f"{'r.skew':>9}{'->tenor':>9}{'i.skew':>9}{'r.kurt':>9}{'->tenor':>9}{'i.kurt':>9}")
        for r in rows:
            if r.observations == 0:
                print(f"  {r.tenor:<6}  {r.warnings[0] if r.warnings else 'unavailable'}")
                continue
            f = lambda v: "—" if v is None or v != v else f"{v:+9.3f}"
            print(f"  {r.tenor:<6}{r.window_days:>7.0f}{r.observations:>5d}{pct(r.realized)}"
                  f"{r.realized_basis:>9}{pct(r.realized_spot)}{pct(r.points_vol)}"
                  f"{pct(r.implied)}{sgn(r.premium)}{f(r.realized_skew)}{f(r.realized_skew_scaled)}"
                  f"{f(r.implied_skew)}{f(r.realized_kurtosis)}{f(r.realized_kurtosis_scaled)}"
                  f"{f(r.implied_kurtosis)}")
        print("  'on' is the return series: a quoted volatility is a volatility of the forward, so "
              "where the\n  sheet quotes swap points their moves are realized volatility too. "
              "'pts vol' is that part alone;\n  'on spot' is the same window measured without it.")

        if args.sabr:
            # The risk reversal and the butterfly cannot be held up against a
            # realized moment -- a quoted spread is not a moment.  What they
            # share with history is SABR's two parameters, so both sides are
            # said in those.
            tag = f"{int(round(args.sabr_delta * 100))}"
            print(f"\nwings as SABR shape — {tag} delta, marked smile against measured history")
            print(f"  {'tenor':<6}{'rr':>9}{'fly':>9}{'i.rho':>9}{'r.rho':>9}{'±se':>7}"
                  f"{'diff':>9}{'i.nu':>9}{'r.nu':>9}{'±se':>7}{'diff':>9}"
                  f"{'i.nu*sqrt(t)':>13}{'measured on':>13}{'window':>7}{'fit err':>9}")
            for r in rows:
                if r.implied_rho is None and r.realized_rho is None:
                    print(f"  {r.tenor:<6}  {'; '.join(r.warnings[:1]) or 'unavailable'}")
                    continue
                g = lambda v, w=9, d=3: "—".rjust(w) if v is None or v != v else f"{v:>+{w}.{d}f}"
                nu_scaled = None if r.implied_nu is None else r.implied_nu * math.sqrt(r.t)
                print(f"  {r.tenor:<6}{sgn(r.marked_rr)}{sgn(r.marked_fly)}"
                      f"{g(r.implied_rho)}{g(r.realized_rho)}{g(r.realized_rho_se, 7)}"
                      f"{g(r.rho_difference)}{g(r.implied_nu)}{g(r.realized_nu)}"
                      f"{g(r.realized_nu_se, 7)}{g(r.nu_difference)}{g(nu_scaled, 13)}"
                      f"{(r.dynamics_source or '—'):>13}"
                      f"{('—' if r.dynamics_days is None else f'{r.dynamics_days:.0f}d'):>7}"
                      f"{('—'.rjust(9) if r.implied_shape_error is None else f'{r.implied_shape_error * 100:>9.4f}')}")
            print("  rho is the spot/volatility correlation a risk reversal is paid for, nu the vol "
                  "of vol a\n  butterfly is paid for. SABR has no mean reversion, so nu rises at "
                  "short tenors on both sides;\n  nu*sqrt(t) is the scale-free number that "
                  "actually sets the shape. 'fit err' is how far the\n  marked smile is from any "
                  "SABR smile at all, in volatility points — a large one means these\n  two "
                  "parameters do not describe it and the comparison is loose. 'window' is what "
                  "the\n  measured pair was taken over, and it is deliberately not the realized "
                  "lookback: rho and nu\n  are properties of the process rather than forecasts "
                  "over a horizon, and they need more\n  paired observations than a realized "
                  "volatility needs returns.")

    if book.data.pairs[args.pair].is_cross:
        tri = analytics.triangle_table(book, args.pair, method=args.method, cut=args.cut,
                                       with_noise=not args.no_noise)
        legs = book.data.pairs[args.pair].legs
        print(f"\ntriangle — {args.pair} against {legs[0]} and {legs[1]} "
              f"(coefficients {tri[0].coefficients if tri else '?'})")
        keys = ["atm", "rr25", "fly25", "rr10", "fly10"]
        # The vega split is per unit of at-the-money vega on the cross, so it
        # is a ratio and not a volatility: printed plain, not as a percentage.
        def ratio(v, w=9):
            return "—".rjust(w) if v is None or v != v else f"{v:>{w}.3f}"

        print(f"  {'tenor':<6}{'rho':>7}{'implied rho':>12}"
              f"{'vega ' + legs[0]:>15}{'vega ' + legs[1]:>15}{'per rho':>9}" +
              "".join(f"{k + ' mk':>11}{k + ' tri':>11}{'diff':>9}" for k in keys))
        for r in tri:
            line = (f"  {r.tenor:<6}{r.rho:>+7.3f}"
                    f"{(r.implied_correlation if r.implied_correlation is not None else float('nan')):>+12.3f}"
                    f"{ratio(r.leg_vega[0], 15)}{ratio(r.leg_vega[1], 15)}"
                    f"{sgn(r.rho_vega, 3, 9)}")
            for k in keys:
                line += f"{pct(r.marked.get(k), 3, 11)}{pct(r.triangle.get(k), 3, 11)}{sgn(r.difference.get(k), 3, 9)}"
            print(line)
        print(f"  vega columns: one unit of {args.pair} at-the-money vega is that many units in "
              f"each leg -- the two hedges. Weighted by each leg's own volatility they account "
              f"for the whole of {args.pair}'s exactly; they do not add to one and are not "
              f"meant to. The correlation carries its own risk, worth 'per rho' volatility "
              f"points of {args.pair} for a whole unit of it, and no leg vega hedges it")
        if tri:
            print(f"  variance triangle ATM {pct(tri[0].variance_triangle_atm)} at {tri[0].tenor}; "
                  f"the distribution triangle sits {sgn(tri[0].smile_convexity)} above it, which is "
                  f"the convexity of the legs' own smiles")
            noise = {k: max(r.noise.get(k, 0.0) for r in tri) for k in keys if any(k in r.noise for r in tri)}
            if noise:
                print("  noise floor (the same machinery run on each leg alone): "
                      + ", ".join(f"{k} {v * 100:.4f}" for k, v in noise.items()))
        for r in tri:
            for w in r.warnings:
                print(f"  ! {r.tenor}: {w}")

    if args.relative_value:
        # The same function the screen calls, so a cell quoted off the grid
        # can be reproduced here and taken apart signal by signal.
        from . import relvalue

        # Through the panel the screen posts, not around it, so the two
        # cannot read one command line's worth of settings differently.
        grid = relvalue.panel_from_request({
            "pair": args.pair, "cut": args.cut, "method": args.method,
            "horizon_days": args.horizon, "lookback_days": args.lookback,
            "history_days": args.history_days, "annualisation": args.annualisation,
            "realized_basis": args.realized_basis, "triangle": True,
            "weights": _weights(args.weight),
        }).run(book, history)
        cols = [c["name"] for c in grid.columns]
        labels = {c["name"]: c["label"] for c in grid.columns}
        print(f"\nrelative value — {grid.history_days:g}-day history, weights " +
              " / ".join(f"{k} {v:g}" for k, v in grid.weights.items()))

        def sd(v, w=10):
            return "—".rjust(w) if v is None or v != v else f"{v:>+{w}.2f}"

        def grid_block(title: str, pick, fmt) -> None:
            print(f"  {title}")
            print(f"  {'tenor':<6}" + "".join(f"{labels[c]:>11}" for c in cols))
            for r in grid.rows:
                by = {c.column: c for c in r.cells}
                print(f"  {r.tenor:<6}" +
                      "".join(fmt(pick(by[c])) if c in by else "—".rjust(11) for c in cols))

        grid_block("score — standard deviations of this cell's own history, + is rich",
                   lambda c: c.score, lambda v: sd(v, 11))
        grid_block("richness — volatility points: implied less realized, roll and carry",
                   lambda c: c.richness, lambda v: sgn(v, 3, 11))

        # Which regime each tenor is in, before the carry signal is read.
        if any(r.regime_z is not None for r in grid.rows):
            print("  regime — the forward's drift in standard deviations of the option's own "
                  "diffusion")
            print(f"  {'tenor':<6}{'spot':>10}{'forward':>10}{'carry/yr':>10}{'atm':>8}"
                  f"{'z':>8}{'z realzd':>10}{'c/sigma':>9}{'horizon':>10}  regime")
            for r in grid.rows:
                if r.regime_z is None:
                    continue
                horizon = ("—".rjust(10) if r.carry_horizon_days is None
                           else f"{r.carry_horizon_days / 365.2425:>9.1f}y")
                g = lambda v, w=8, d=3: "—".rjust(w) if v is None else f"{v:>{w}.{d}f}"
                print(f"  {r.tenor:<6}{r.spot:>10.5f}{r.forward:>10.5f}"
                      f"{sgn(r.carry_drift, 2, 10)}{pct(r.atm, 3, 8)}{g(r.regime_z)}"
                      f"{g(r.regime_z_realized, 10)}{g(r.carry_to_vol, 9)}{horizon}  "
                      + ("carry dominated" if r.carry_dominant else "diffusion"))
            print(f"  z crosses {relvalue.CARRY_DOMINANT_Z:g} at the 'horizon' beside it "
                  f"(T = {relvalue.CARRY_HORIZON_FACTOR:g}*sigma^2/c^2), and past it the "
                  f"position is\n  mostly a carry trade in an option's clothes. The weight is "
                  "not tapered on the strength of\n  this: the weights are yours, and a score "
                  "that quietly reweighted itself would be a\n  different statistic on every "
                  "row with nothing on the screen to say so.")
            m = grid.managed
            if m.get("carry_to_vol") is not None:
                print(f"  pair reading: median carry {m['carry_to_vol']:.2f} of realized "
                      f"volatility, median realized {m['realized'] * 100:.2f}% — "
                      + ("the managed-float shape (both conditions hold)" if m["managed"]
                         else "not the managed-float shape"))

        # What the level signal is measured against, taken apart.  The
        # defensible "the carry supports the volatility" number is the ratio
        # of the two realized volatilities, not the level of the swap points,
        # so the ratio is the column and the points are beside it.
        if any(r.realized is not None for r in grid.rows):
            print("  realized — what the level signal is measured against")
            print(f"  {'tenor':<6}{'window':>8}{'obs':>5}{'on':>9}{'real':>9}{'spot':>9}"
                  f"{'fwd':>9}{'pts':>9}{'corr':>8}{'carry/yr':>10}{'fwd/spot':>10}")
            for r in grid.rows:
                if r.realized is None:
                    continue
                ratio = ("—".rjust(10) if r.forward_vol_ratio is None
                         else f"{r.forward_vol_ratio:>10.4f}")
                corr = ("—".rjust(8) if r.points_correlation is None
                        else f"{r.points_correlation:>+8.3f}")
                print(f"  {r.tenor:<6}{(r.window_days or 0):>8.0f}{r.observations:>5d}"
                      f"{r.realized_basis:>9}{pct(r.realized)}{pct(r.realized_spot)}"
                      f"{pct(r.realized_forward)}{pct(r.points_vol)}{corr}"
                      f"{sgn(r.realized_carry_rate, 2, 10)}{ratio}")
            print("  fwd/spot is the realized volatility of the forward over the realized "
                  "volatility of\n  spot. Near one, the points moved with spot and the level "
                  "comparison rests on the same\n  variance either way; well above one, the "
                  "swap points are carrying variance of their\n  own and the level signal "
                  "deserves a second look. The level of 'carry/yr' says nothing\n  about that "
                  "on its own, which is why the ratio is the column and not the points.")

        names = [n for n, _ in relvalue.SIGNALS]
        # A shared signal is one number for the whole tenor and is marked, so
        # a column that repeats five times down the table is read as one
        # observation and not five agreeing ones.
        head = {n: (n + "*" if n in relvalue.SHARED else n) for n in names}
        print("  attribution — volatility points, and the z the score actually used")
        print(f"  {'tenor':<6}{'point':<9}{'impl':>8}" +
              "".join(f"{head[n]:>9}{'z':>7}" for n in names) +
              f"{'score':>8}{'scale':>8}{'conf':>7}  used")
        for r in grid.rows:
            for c in r.cells:
                by = {s.name: s for s in c.signals}
                line = f"  {r.tenor:<6}{c.label:<9}{pct(c.implied, 3, 8)}"
                for n in names:
                    sig = by.get(n)
                    line += (sgn(None if sig is None else sig.value, 3, 9) +
                             ("—".rjust(7) if sig is None or sig.z is None or not sig.used
                              else f"{sig.z:>+7.2f}"))
                scale = "—".rjust(8) if c.scale is None else f"{c.scale * 100:>8.4f}"
                print(line + sd(c.score, 8) + scale + f"{c.confidence * 100:>6.0f}%" + "  " +
                      (",".join(c.used) if c.used else "—") +
                      ("" if not c.scale_source or c.scale_source == c.column
                       else f" (scale from {c.scale_source})"))
        if any(n in relvalue.SHARED for n in names):
            marked = ", ".join(n for n in names if n in relvalue.SHARED)
            print(f"  * {marked} is a property of the tenor and not of the strike: the same "
                  f"number in all five\n  rows of an expiry, so it is one observation and not "
                  f"five agreeing ones. Its z still\n  differs by cell, because each cell "
                  f"divides by its own scale.")
        print(f"  {grid.summary['headline']}")
        # Every reason a cell is missing a signal, said once.  Repeating it
        # forty-five times would bury the one that matters.
        notes = dict.fromkeys(
            [w for r in grid.rows for w in r.warnings] +
            [r.message for r in grid.rows if r.message] +
            [s.message for r in grid.rows for c in r.cells for s in c.signals if s.message] +
            [c.message for r in grid.rows for c in r.cells if c.message] +
            list(grid.warnings) + list(grid.unavailable.values()))
        for note in notes:
            print(f"  . {note}")
        print("  the score divides each signal by how much this very cell's volatility "
              "usually moves,\n  so half a point on a one-year at-the-money and half a point "
              "on a one-week wing are not\n  the same reading. A cell with no history has no "
              "scale and no score — the volatility\n  points beside it are still measured.")

    if args.compare:
        # The panel moved to the Monitor screen, and its command moved with
        # it: a build that has the monitor tab but not this one must still be
        # able to run a comparison.  argparse would say "unrecognised
        # argument", which is a worse answer than the address.
        raise ValueError(
            "the curve comparison moved to the Monitor screen. Run "
            f"'volkit monitor {args.pair} "
            + " ".join(f"--compare {c}" for c in args.compare)
            + f" --field {args.field}' instead")
    return 0


def cmd_monitor(args) -> int:
    """What has moved: one small panel per pair, and the curve comparison.

    The web screen's Monitor tab and this command share
    ``monitor.panel_from_request`` and ``curves.ComparePanel``, so a screen
    set up in the browser and the same screen run from a shell script produce
    identical numbers.
    """
    from . import monitor as monitor_mod
    from .history import load_history

    specs = list(args.watch)
    if not specs:
        if not args.pair:
            raise ValueError("nothing to watch: give a pair, or --watch PAIR[:was[:now]]")
        specs = [args.pair]
    tiles = [monitor_mod.parse_spec(x) for x in specs]

    pairs = sorted({t.pair for t in tiles}
                   | ({args.pair} if args.pair else set())
                   | {c.pair for c in
                      (curves_spec(args) if args.compare else [])})
    book = _book(args, pairs)

    history = None
    if args.history:
        history = load_history(args.history, book.pairs, vol_unit=args.vol_unit)
        for p in list(history.problems) + list(history.skipped_sheets):
            print(f"  . {p}", file=sys.stderr)

    panel = monitor_mod.MonitorPanel(tiles=tuple(tiles), cut=args.cut, method=args.method,
                                     field=args.field)
    r = panel.run(book, history)
    from .curves import CURVE_FIELDS, FIELD_LABELS
    keys = list(CURVE_FIELDS)
    print(f"monitor — valuation {book.clock.now:%Y-%m-%d %H:%M}Z, cut {args.cut}, "
          f"{args.method}; changes in volatility points")
    for tile in r["tiles"]:
        print(f"\n{tile['label']}")
        if not tile["ok"]:
            print(f"  UNAVAILABLE: {tile['message']}")
            continue
        print(f"  was: {tile['was']['source'] or tile['was']['kind_label']}")
        print(f"  now: {tile['now']['source'] or tile['now']['kind_label']}")
        print(f"  {'tenor':<7}" + "".join(f"{k:>11}" for k in keys)
              + f"   {FIELD_LABELS[args.field]} was -> now")
        for row in tile["rows"]:
            line = f"  {row['tenor']:<7}"
            for k in keys:
                v = row["change"][k]
                line += "—".rjust(11) if v is None else f"{v * 100:>+11.4f}"
            was, now = row["was"][args.field], row["now"][args.field]
            line += ("   " + ("—" if was is None else f"{was * 100:.4f}") + " -> "
                     + ("—" if now is None else f"{now * 100:.4f}"))
            print(line)
        for note in tile["notes"]:
            print(f"  . {note}")
        for w in tile["warnings"][:4]:
            print(f"  ! {w}")

    if args.compare:
        _print_comparison(args, book, history)
    return 0


def curves_spec(args):
    """The ``--compare`` arguments as curve requests, with the pair filled in."""
    from . import curves as curves_mod
    return [curves_mod.parse_spec(spec, args.pair or "") for spec in args.compare]


def _print_comparison(args, book, history) -> None:
    """The curve comparison panel, from the same function the screen calls.

    ``--compare`` is repeatable: ``--compare surface --compare marks
    --compare history:-30d`` is three curves, differenced against the first.
    """
    from . import curves as curves_mod

    panel = curves_mod.ComparePanel(
        curves=tuple(curves_spec(args)),
        cut=args.cut, method=args.method, field=args.field, base=0)
    r = panel.run(book, history)

    field = r["field"]
    width = max([11] + [len(c["label"]) for c in r["curves"]]) + 2

    def cell(curve, tenor, key):
        point = next((p for p in curve["points"] if p["tenor"].upper() == tenor), None)
        v = None if point is None else point[key].get(field)
        if key == "diffs" and curve["is_base"]:
            return f"{'base':>{width}}"
        if v is None:
            return f"{'—':>{width}}"
        return f"{v * 100:>{width}.4f}" if key == "values" else f"{v * 100:>+{width}.4f}"

    print(f"\ncurve comparison — {curves_mod.FIELD_LABELS[field]}, in volatility points, "
          f"differenced against {r['base_label']!r}")
    print(f"  {'tenor':<9}" + "".join(f"{c['label'][:width]:>{width}}" for c in r["curves"]))
    for tenor in r["tenors"]:
        print(f"  {tenor:<9}" + "".join(cell(c, tenor, "values") for c in r["curves"]))
    print()
    for tenor in r["tenors"]:
        print(f"  {'d ' + tenor:<9}" + "".join(cell(c, tenor, "diffs") for c in r["curves"]))
    for c in r["curves"]:
        state = "" if c["ok"] else f"  UNAVAILABLE: {c['message']}"
        print(f"  . {c['label']}: {c['source'] or c['kind_label']}{state}")
        for w in c["warnings"]:
            print(f"    ! {w}")
    for note in r["notes"]:
        print(f"  . {note}")


def cmd_listed(args) -> int:
    """Fit a SABR curve to a pasted exchange strike/volatility table.

    The web screen's Exchange-traded tab and this command share
    ``listed.panel_from_request``, so a panel set up in the browser and the
    same panel run from a shell script produce identical numbers.
    """
    from . import listed as listed_mod

    text = sys.stdin.read() if args.file in (None, "-") else paths.read_text(args.file)
    payload = {
        "underlying": args.underlying, "pair": args.pair,
        "invert": args.invert if args.invert is not None else None,
        "scale": args.scale, "expiry": args.expiry, "forward": args.forward,
        "text": text, "vol_unit": args.vol_unit, "beta": args.beta,
        "alpha": args.alpha, "rho": args.rho, "volvol": args.nu,
        "weighting": args.weighting, "cut": args.cut, "method": args.method,
        "strike_column": args.strike_column, "vol_column": args.vol_column,
        "contract_size": args.contract_size,
    }
    panel = listed_mod.panel_from_request(payload)
    book = None
    if panel.underlying.pair and not args.no_compare:
        book = _book(args, [panel.underlying.pair])
    r = panel.run(book, clock=_clock(args))

    u, f = r["underlying"], r["fit"]
    print(f"{u['code']}  {u['name']}"
          + (f"  ->  {u['pair']}{' (inverted)' if u['invert'] else ''}" if u["pair"] else ""))
    print(f"  expiry {r['expiry'][:16].replace('T', ' ')}Z   t={r['years']:.5f}y "
          f"({r['days']:.2f} days)   forward {r['forward']:g}   {r['n_quotes']} quotes")
    # A given parameter is marked where it is printed, not only in the
    # warnings: read off the line on its own it is indistinguishable from one
    # the market implied.
    pinned = set(f.get("fixed") or ())
    tag = lambda k: "*" if k in pinned else " "
    print(f"  SABR   alpha={f['alpha']:.6f}{tag('alpha')} rho={f['rho']:+.4f}{tag('rho')} "
          f"nu={f['volvol']:.4f}{tag('volvol')}(nu*sqrt(t)={f['log_volvol']:.4f})  "
          f"beta={f['beta']:g}" + ("   * = given, not fitted" if pinned else ""))
    print(f"  fit    vol at forward {f['atm_vol']:.4f}%   weighted RMSE {f['rmse']:.4f} vol pts   "
          f"worst {f['max_error']:+.4f} at {f['max_error_strike']:g}   [{f['message']}]")

    has_book = r["comparison"] is not None
    head = f"  {'strike':>14}{'mkt %':>10}{'fit %':>10}{'fit-mkt':>9}"
    if has_book:
        head += f"{'book %':>10}{'fit-book':>10}"
    print("\n" + head)
    for row in r["rows"]:
        line = (f"  {row['strike']:>14g}{row['market_vol']:>10.4f}{row['fit_vol']:>10.4f}"
                f"{row['fit_diff']:>+9.4f}")
        if has_book:
            line += f"{row['book_vol']:>10.4f}{row['book_diff']:>+10.4f}"
        print(line)

    if has_book:
        c = r["comparison"]
        print(f"\n  against the marked {c['pair']} surface  (cut {c['cut']}, {c['method']}), "
              f"read off both curves at the book's own delta strikes:")
        print(f"  {'point':<10}{'listed K':>14}{'fx K':>12}{'book %':>10}{'listed %':>10}{'diff':>9}")
        for a in c["anchors"]:
            print(f"  {a['label']:<10}{a['listed_strike']:>14g}{a['fx_strike']:>12.5f}"
                  f"{a['book_vol']:>10.4f}{a['fit_vol']:>10.4f}{a['diff']:>+9.4f}")
        for key in ("rr25", "fly25", "rr10", "fly10"):
            if key in c["rr_book"]:
                print(f"  {key:<10}{'':>26}{c['rr_book'][key]:>10.4f}"
                      f"{c['rr_listed'][key]:>10.4f}{c['rr_listed'][key] - c['rr_book'][key]:>+9.4f}")

    for n in r["notes"]:
        print(f"  . {n}")
    for w in r["warnings"]:
        print(f"  ! {w}")

    if args.positions:
        # The same call the screen makes, with this command's panels as the
        # whole screen: a position naming another contract is told so rather
        # than being priced against the only curve to hand.  --panels is what
        # lets a screen holding several contracts be reproduced here, since
        # aggregating across them is the whole point of the positions table.
        panels = [payload]
        if args.panels:
            more = json.loads(paths.read_text(args.panels))
            if not isinstance(more, list):
                raise ValueError(f"{args.panels} must hold a JSON list of panels, "
                                 f"got {type(more).__name__}")
            panels += more
        g = listed_mod.positions_from_request({
            "text": (sys.stdin.read() if args.positions == "-"
                     else paths.read_text(args.positions)),
            "panels": panels, "vol_bump": args.vol_bump, "theta_days": args.theta_days,
        }).run(clock=_clock(args))
        _print_listed_positions(g)
        return 1 if (r["warnings"] or g["warnings"]) else 0
    return 1 if r["warnings"] else 0


def _m(v) -> str:
    """One money cell, blank when there is no number to print.

    A money total across two settlement currencies is not a number, so it is
    reported as absent rather than as a sum -- the per-currency rows beside it
    hold every figure.
    """
    return "--" if v is None else f"{v:,.0f}"


def _print_listed_positions(g: dict) -> None:
    """The positions table and the aggregate, in the screen's own order."""
    print(f"\n  positions   vega, vanna and volga per {g['vol_bump']:g} vol point(s); "
          f"theta over {g['theta_days']:g} day(s); money in the contract's settlement currency")
    head = (f"  {'#':>3} {'panel':<12}{'strike':>12}{'c/p':>5}{'qty':>9}{'vol %':>9}"
            f"{'premium':>13}{'delta fut':>11}{'vega':>12}{'theta':>11}{'gamma 1%':>11}")
    print(head)
    for kind in ("bs", "smile"):
        print(f"  -- {'Black-Scholes' if kind == 'bs' else 'smile (revalued on the fit)'}")
        for row in g["positions"]:
            if row["bs"]["vega"] is None:
                print(f"  {row['line']:>3} {row['panel'][:12]:<12}"
                      f"{'':>12}{'':>5}{row['quantity']:>9g}   ! {row['error']}")
                continue
            k = row[kind]
            print(f"  {row['line']:>3} {row['panel'][:12]:<12}{row['strike']:>12g}"
                  f"{('C' if row['type'] == 'call' else 'P'):>5}{row['quantity']:>9g}"
                  f"{row['vol']:>9.4f}{row['premium']:>13,.0f}{k['delta_futures']:>11,.2f}"
                  f"{k['vega']:>12,.0f}{k['theta']:>11,.0f}{k['gamma_1pct']:>11,.0f}")
        for grp in g["groups"]:
            k = grp[kind]
            print(f"      {'= ' + grp['panel'][:10]:<12}{'':>12}{'':>5}{'':>9}{'':>9}"
                  f"{grp['premium']:>13,.0f}{k['delta_futures']:>11,.2f}"
                  f"{k['vega']:>12,.0f}{k['theta']:>11,.0f}{k['gamma_1pct']:>11,.0f}")
        # Per contract, every column still adds; the row is only worth
        # printing where more than one panel is under the code.
        for con in g.get("contracts") or ():
            if con["panels"] < 2:
                continue
            k = con[kind]
            print(f"      {'= ' + con['underlying'][:10]:<12}{'':>12}{'':>5}{'':>9}{'':>9}"
                  f"{con['premium']:>13,.0f}{k['delta_futures']:>11,.2f}"
                  f"{k['vega']:>12,.0f}{k['theta']:>11,.0f}{k['gamma_1pct']:>11,.0f}"
                  f"   ({con['panels']} panels)")
        # Across contracts only money adds, and only within one settlement
        # currency: a sum of euros and dollars is not a number.
        for cc in (g.get("currencies") or [dict(g["totals"], ccy="")]):
            t, label = cc[kind], f"= all {cc['ccy']}".rstrip()
            print(f"      {label:<12}{'':>12}{'':>5}{'':>9}{'':>9}{_m(cc['premium']):>13}"
                  f"{'--':>11}{_m(t['vega']):>12}{_m(t['theta']):>11}"
                  f"{_m(t['gamma_1pct']):>11}")
    print("      futures-equivalent delta is totalled per contract, across that contract's "
          "expiries, and never across contracts: a future of one contract is not a future "
          "of another, and two delivery months are not the same future either")
    for x in g["skipped"]:
        print(f"  ! line {x['line']} skipped ({x['why']}): {x['text']}")
    for n in g["notes"]:
        print(f"  . {n}")
    for w in g["warnings"]:
        print(f"  ! {w}")


def cmd_mm(args) -> int:
    """Fit a curve and the wings to a market, and quote what is being asked for.

    Two stages and two panels, exactly as the screen has them (§11): the fit
    reads the market paste and moves the marks, the quote reads the request
    file and puts a two-way on each line of it.  Both run here when both are
    given, and the fit's marks are handed to the quote the same way the
    browser hands them over -- so this command and the screen stand on the
    same numbers.  Either half runs on its own: a market with no request file
    fits and reports, a request file with ``--no-curve --no-wings`` quotes off
    the marks as they stand.

    The web screen's Market-maker tab and this command share
    ``marketmaker.panel_from_request`` and ``.quote_panel_from_request``, so a
    panel set up in the browser and the same panel run from a shell script
    produce identical numbers.
    """
    from . import marketmaker as mm
    from .knowledge import KnowledgeBank

    if args.file == "-" and args.request == "-":
        print("  ! only one of --file and --request can be read from stdin", file=sys.stderr)
        return 2
    # stdin is the market when nothing says otherwise, which is what
    # ``volkit mm EURUSD < run.txt`` has always meant.
    if args.file == "-" or (args.file is None and args.request is None):
        market_text = sys.stdin.read()
    elif args.file:
        market_text = paths.read_text(args.file)
    else:
        market_text = ""
    if args.request == "-":
        request_text = sys.stdin.read()
    elif args.request:
        request_text = paths.read_text(args.request)
    else:
        request_text = ""

    common = {
        "pair": args.pair, "cut": args.cut, "method": args.method,
        "vol_unit": args.vol_unit, "fly_convention": args.fly, "text": market_text,
    }
    fit_payload = {
        **common,
        "target_source": args.target_source,
        "target_text": (paths.read_text(args.target) if args.target else ""),
        "fit_curve": not args.no_curve, "free": args.free,
        "tune_wings": not args.no_wings, "smile_free": args.smile_free,
        "mid_pull": args.mid_pull, "max_nfev": args.max_evals,
        "apply": False,
    }
    quote_payload = {
        **common,
        "request_text": request_text,
        "vega_text": (paths.read_text(args.vega) if args.vega else ""),
        "vega_scale": args.axe_scale, "fair_weight": args.fair_weight,
        "axe_weight": args.axe_weight, "skew_cap": args.skew_cap,
        "horizon_days": args.horizon, "fallback_spread": args.fallback_spread,
        "use_archive_width": bool(args.archive_width),
    }
    bank = KnowledgeBank.load(args.knowledge)
    clock = _clock(args)
    arc = None
    if args.archive_width:
        from . import archive as archive_mod
        arc = archive_mod.Archive.load(args.archive)
        for problem in arc.problems:
            print(f"  ! archive: {problem}", file=sys.stderr)

    if args.learn:
        book = Book.from_excel(args.workbook, clock)
        rules, notes, parse = mm.learn_from_panel(fit_payload, clock)
        print(f"{args.pair}: {parse['n_quotes']} quote(s) read, {parse['vol_unit']}")
        for r in rules:
            print(f"  {r.describe()}")
            if r.text:
                print(f"      {r.text}")
        for n in notes:
            print(f"  . {n}")
        for row in parse["skipped"]:
            print(f"  ! line {row['line']} skipped ({row['why']}): {row['text'][:60]}")
        if not args.save:
            print("\n  nothing was written; add --save to put these into the bank")
            return 0
        merged, merge_notes = mm.merge_rules(list(bank.for_pair(args.pair).rules), rules)
        problems = bank.set_pair(args.pair, merged, clock.now,
                                 f"learned from a paste of {parse['n_quotes']} quotes")
        if problems:
            for x in problems:
                print(f"  ! {x}", file=sys.stderr)
            return 2
        print(f"\n  wrote {bank.save(args.knowledge)}")
        for n in merge_notes:
            print(f"  . {n}")
        return 0

    # A fit needs something to aim at: a market to read the at-the-money off,
    # or a target curve that does not come out of one.  Neither, and there is
    # nothing to fit and the request is quoted off the marks as they stand.
    do_fit = bool(market_text.strip()) or args.target_source not in ("quotes", "none")
    do_quote = bool(request_text.strip())
    if not do_fit and not do_quote:
        print("  ! nothing to do: paste a market to fit to (--file), or a list of instruments "
              "to quote (--request)", file=sys.stderr)
        return 2

    book = _book(args, [args.pair])
    if args.feed:
        # The screen has a feed loaded, so this must be able to have one too:
        # a quote written against an absolute strike needs the outright forward
        # to become a moneyness, and without it every strike row on the sheet
        # comes back unavailable.
        from .feed import MarketFeed
        try:
            book.feed = MarketFeed.load(args.feed)
        except Exception as exc:  # noqa: BLE001 - the delta quotes still price
            print(f"  ! forward feed: {exc}", file=sys.stderr)
    hist = None
    if args.history:
        from .history import load_history
        loaded = load_history(args.history)
        if args.pair in loaded:
            hist = loaded[args.pair]
        else:
            print(f"  ! {args.history} has no sheet for {args.pair}", file=sys.stderr)

    warnings = 0
    if do_fit:
        fit = mm.panel_from_request(fit_payload).run(book)
        _print_fit(fit, do_quote)
        warnings += len(fit["warnings"])
        # The same hand-off the browser makes, for the same reason: the fit's
        # answer travels as numbers and is put on the surface for the length
        # of one quote run.
        quote_payload["marks"] = fit["marks"]
    if do_quote:
        out = mm.quote_panel_from_request(quote_payload).run(book, bank=bank, hist=hist,
                                                             archive=arc)
        _print_quote(out, float(args.skew_cap))
        warnings += len(out["warnings"])
    return 1 if warnings else 0


def _cell(value, width=10, dp=4, signed=False):
    if value is None:
        return f"{'-':>{width}}"
    return f"{value:>+{width}.{dp}f}" if signed else f"{value:>{width}.{dp}f}"


def _print_fit(r: dict, quoting: bool) -> None:
    """The fit: what moved, and where the surface now sits against the market."""
    print(f"{r['pair']}  cut {r['cut']}  {r['method']}  "
          f"valuation {r['valuation'][:16].replace('T', ' ')}Z"
          + ("   [cross]" if r["is_cross"] else ""))

    c = r["curve"]
    if c is None:
        print(f"\n  curve: {r['unavailable'].get('curve', 'not fitted')}")
    else:
        print(f"\n  curve   target: {c['evidence']}")
        print(f"          free: {', '.join(c['free'])}   [{c['message']}]   "
              f"rmse {c['rmse']:.4f} vol pts, worst {c['max_error']:+.4f} at {c['max_error_tenor']}")
        print(f"  {'tenor':<8}{'target':>9}{'before':>9}{'after':>9}{'miss':>8}{'moved':>8}")
        for row in c["rows"]:
            print(f"  {row['tenor']:<8}{row['target']:>9.4f}{row['before']:>9.4f}"
                  f"{row['after']:>9.4f}{row['diff']:>+8.4f}{row['moved']:>+8.4f}")
        for k in c["after"]:
            print(f"      {k:<16}{c['before'][k]:>12.5f} ->{c['after'][k]:>12.5f}")
        for w in c["warnings"]:
            print(f"  ! {w}")

    w = r["wings"]
    if w is None:
        print(f"\n  wings: {r['unavailable'].get('wings', 'not tuned')}")
    else:
        print(f"\n  wings   {w['inside_before']} -> {w['inside_after']} of {w['quotes']} quote(s) "
              f"inside their market; worst miss {w['worst_before']:.4f} -> {w['worst_after']:.4f} "
              f"vol pts   [{w['message']}]")
        print("      " + "   ".join(f"{k} {w['before'][k]:+.5f} -> {w['after'][k]:+.5f}"
                                    for k in w["after"]))
        print(f"      {w['evaluations']} evaluations, {w['slices']} smile fits, "
              f"{w['seconds']:.2f}s")

    market = r["market"]
    print(f"\n  {market['n_quotes']} quote(s) read, {market['inside_before']} with the model "
          f"inside their market before the fit and {market['inside']} after")
    # The time column only appears when the paste was timed; a column of dashes
    # on a run nobody timestamped is noise in a table that is already wide.
    timed = any(row.get("timestamp") for row in market["rows"]) or bool(market.get("superseded"))
    stamp_w = max([5] + [len(row.get("timestamp") or "") for row in market["rows"]]) + 2
    head = f"  {'quote':<26}"
    if timed:
        head += f"{'time':>{stamp_w}}"
    print(head + f"{'their bid':>10}{'their ask':>10}{'was':>9}{'now':>9}{'moved':>9}  verdict")
    for row in market["rows"]:
        stamp = f"{(row.get('timestamp') or '-'):>{stamp_w}}" if timed else ""
        print(f"  {row['describe']:<26}{stamp}{_cell(row['market_bid'])}{_cell(row['market_ask'])}"
              f"{_cell(row['model_before'], 9)}{_cell(row['model_after'], 9)}"
              f"{_cell(row['model_move'], 9, 4, True)}  {row['verdict']}")
        for x in row["warnings"]:
            print(f"      ! {x}")
    for n_ in market["notes"]:
        print(f"  . {n_}")
    for row in market["skipped"]:
        print(f"  ! line {row['line']} skipped ({row['why']}): {row['text'][:60]}")
    for row in market.get("superseded") or []:
        # Not an error and not skipped: read, understood, and replaced by a
        # later quote of the same thing.  Printed so a run whose update was
        # mis-typed shows the quote that went missing rather than hiding it.
        print(f"  . line {row['line']} superseded by line {row['replaced_by']}: "
              f"{row['describe']} {row['bid']:.4f}/{row['ask']:.4f}"
              + (f" at {row['timestamp']}" if row["timestamp"] else ""))
    marks = r["marks"]
    print(f"  . the fit moved {marks['what']}"
          + ("; those marks are what the quote below stands on" if quoting else
             ", and put the book back. Add --request to quote off what it arrived at"))
    for x in r["warnings"]:
        print(f"  ! {x}")


def _print_quote(r: dict, panel_cap: float) -> None:
    """The quote: a two-way in each instrument that was asked for."""
    sheet = r["sheet"]
    print(f"\n  {sheet['n_quotes']} instrument(s) asked for, {sheet['priced']} with a width, "
          f"{sheet['matched']} also quoted in the market paste")
    print(f"  . {r['marks']['note']}")
    ar = r.get("archive") or {}
    if ar.get("used"):
        print("  . " + (f"archive on the width ladder: {ar.get('widths', 0)} width(s) held "
                        f"with enough behind them, {ar.get('counted', 0)} observation(s) counted"
                        if ar.get("available") else ar.get("reason", "")))
    print(f"  {'instrument':<26}{'their bid':>10}{'their ask':>10}{'model':>9}{'skew':>9}"
          f"{'our bid':>9}{'our ask':>10}{'width':>8}  verdict")
    capped = 0
    for row in sheet["rows"]:
        # A starred lean is one the cap bound: the axe wanted to move the price
        # further than a quote is allowed to be moved.
        if row["skew_capped"]:
            capped += 1
        print(f"  {row['describe']:<26}{_cell(row['market_bid'])}{_cell(row['market_ask'])}"
              f"{_cell(row['model'], 9)}{_cell(row['skew_total'], 8, 3, True)}"
              f"{'*' if row['skew_capped'] else ' '}"
              f"{_cell(row['our_bid'], 9)}{_cell(row['our_ask'])}{_cell(row['width'], 8, 3)}"
              f"  {row['verdict']}")
        if row["width_source"]:
            print(f"      . width: {row['width_source']}")
        if row["skew_reason"]:
            print(f"      . {row['skew_reason']}")
        for x in row["advice"]:
            print(f"      > {x}")
        for x in row["notes"]:
            print(f"      . {x}")
        for x in row["warnings"]:
            print(f"      ! {x}")
    if capped:
        print(f"  . * {capped} row(s) had the lean capped at {panel_cap:g} times the half width; "
              f"an axe may lean a price inside the market but not walk it out of one")
    for n_ in sheet["notes"]:
        print(f"  . {n_}")
    for row in sheet["skipped"]:
        print(f"  ! line {row['line']} skipped ({row['why']}): {row['text'][:60]}")
    for x in r["warnings"]:
        print(f"  ! {x}")


def cmd_validate(args) -> int:
    """Re-fit every smile hunting for competing solutions.

    The three FX quotes do not always determine a smile uniquely (Healy, 2025).
    The routine fit takes the global best and stops; this sweeps for other
    parameter sets that reprice the same quotes just as well, which is the
    thing to check before trusting a fit that moved unexpectedly.
    """
    from . import sabr
    book = _book(args, [args.pair] if args.pair else None)
    pairs = [args.pair] if args.pair else book.pairs
    total = flagged = 0
    for name in pairs:
        surface = book.surfaces.get(name)
        marks = book.data.marks.get(name)
        if surface is None or not marks:
            continue
        for mark in marks:
            t = tenor_to_years(mark.tenor)
            atm = surface.atm.cut_vol(book.clock.datetime_from_years(t), "NY") or surface.atm.term_vol(t)
            for wing, rr, st, d in (("25d", mark.rr_25, mark.st_25, 0.25),
                                    ("10d", mark.rr_10, mark.st_10, 0.10)):
                total += 1
                try:
                    cal = sabr.calibrate(atm, rr, st, d, t, surface.conv, max_solutions=3)
                except Exception as exc:  # noqa: BLE001
                    flagged += 1
                    print(f"  {name} {mark.tenor} {wing}: FAILED {exc}")
                    continue
                for w in cal.warnings:
                    flagged += 1
                    print(f"  {name} {mark.tenor} {wing}: {w}")
    print(f"\nchecked {total} tenor/wing calibrations, {flagged} flagged")
    return 1 if flagged else 0


def cmd_events(args) -> int:
    """List the scheduled economic events volkit would auto-load for a pair."""
    from datetime import timedelta
    book = Book.from_excel(args.workbook, _clock(args))
    start = book.clock.now
    end = start + timedelta(days=args.horizon * 365.2425)
    rows = book.econ.for_pair(args.pair, start, end)
    print(f"{args.pair}: {len(rows)} scheduled events to {end:%Y-%m-%d}  "
          f"(source {book.econ.source})")
    print(f"  {'event':<8}{'when (UTC)':<20}{'bump':>6}  note")
    for e in rows:
        note = "date is a rule of thumb, verify" if e.approximate else e.source
        print(f"  {e.name:<8}{e.when:%Y-%m-%d %H:%M}    {e.bump:>5.2f}  {note}")
    return 0


def cmd_session(args) -> int:
    """Save the marks on a book to a file, put a saved file back, or read one.

    The workbook is never written to -- that is a standing decision, not an
    omission -- so this is the file a morning's marking survives in.  It is the
    same file the Save button on the marking and market-maker screens writes,
    through the same functions.
    """
    from . import session as session_mod

    path = args.path or str(session_mod.default_path())
    if args.show:
        doc = session_mod.load(path)
        print(f"{path}")
        print(f"  written  {doc.get('saved') or 'unknown'}"
              + (f" against {doc.get('workbook')}" if doc.get("workbook") else ""))
        if doc.get("valuation"):
            print(f"  valuation {doc['valuation']}")
        if doc.get("note"):
            print(f"  note     {doc['note']}")
        print(f"  units    {doc.get('units') or 'unstated'}")
        for name, block in sorted((doc.get("pairs") or {}).items()):
            bits = []
            if block.get("atm_overwrites"):
                bits.append(f"{len(block['atm_overwrites'])} ATM overwrite(s)")
            if block.get("smile_overwrites"):
                bits.append(f"{sum(len(v) for v in block['smile_overwrites'].values())} "
                            "smile overwrite(s)")
            if block.get("param_shifts"):
                bits.append(f"{len(block['param_shifts'])} wing shift(s)")
            if block.get("events"):
                bits.append(f"{len(block['events'])} event(s)")
            if block.get("anchor_tenors"):
                bits.append("anchored")
            if block.get("band"):
                bits.append(f"band {block['band'].get('mode')}")
            print(f"  {name:<9}{', '.join(bits) or 'curve only'}")
        return 0

    # --save writes what is on the book now; the book itself may already have
    # a session on it, because --session is honoured by every command.
    book = _book(args, [args.pair] if args.pair else None)
    if args.load:
        out = session_mod.restore(book, path, [args.pair] if args.pair else None)
        print(f"{path}: restored {', '.join(out['applied']) or 'nothing'}")
        for note in out["notes"]:
            print(f"  . {note}")
        for problem in out["problems"]:
            print(f"  ! {problem}")
        return 1 if out["problems"] else 0
    written = session_mod.save(book, path, [args.pair] if args.pair else None, note=args.note)
    doc_pairs = [args.pair] if args.pair else book.pairs
    print(f"saved {len(doc_pairs)} pair(s) to {written}")
    print("  the workbook is unchanged; start with --session "
          f"{written} to price against these marks")
    return 0


def cmd_serve(args) -> int:
    from .webapp import serve
    serve(args.workbook, host=args.host, port=args.port,
          clock=_clock(args), open_browser=not args.no_browser, feed_path=args.feed,
          history_path=getattr(args, "history", None),
          bank_path=getattr(args, "knowledge", None),
          session_path=getattr(args, "session", None),
          auto_reload=getattr(args, "auto_reload", 0.0) or 0.0,
          archive_path=getattr(args, "archive", None),
          agent_chats=getattr(args, "chats", None) or [],
          agent_sdr=getattr(args, "sdr", None) or [],
          ingest_state_path=getattr(args, "ingest_state", None),
          dtcc_proxy=getattr(args, "proxy", None),
          journal_path=getattr(args, "journal", None))
    return 0


def _agent_model(args):
    """The local model this run may use, or ``None`` and why not.

    Built here rather than inside the agent so that *every* action prints the
    same sentence about it.  A build that quietly used a model and a build
    that quietly did not must never look the same from the outside.
    """
    from .llm import LlmError, LocalModel, ModelConfig
    if getattr(args, "no_llm", False):
        return None, "no model: --no-llm was given"
    try:
        cfg = ModelConfig.from_env(backend=getattr(args, "llm_backend", None),
                                   base_url=getattr(args, "llm_url", None),
                                   model=getattr(args, "llm_model", None))
    except LlmError as exc:
        return None, f"no model: {exc}"
    model = LocalModel(cfg)
    if not model.available():
        return None, f"no model: {model.why_not}"
    return model, f"model: {cfg.describe()}"


def _agent_folders(args) -> list[tuple[str, str]]:
    return ([(p, "chat") for p in (getattr(args, "chats", None) or [])]
            + [(p, "sdr") for p in (getattr(args, "sdr", None) or [])])


def cmd_agent(args) -> int:
    """The desk agent: ingest, synthesize, quote, explain, and keep the record.

    Every action here goes through the same functions the screen would call,
    which is what keeps a price made in a shell and a price made in a browser
    the same price -- the rule §10 sets for every screen in this tool.
    """
    import json as _json
    from datetime import timedelta
    from . import agent as agent_mod
    from . import archive as archive_mod
    from . import ingest as ingest_mod
    from . import synthesis as synthesis_mod
    from .knowledge import KnowledgeBank, merge_rules

    action = args.action
    pair = (args.pair or "").upper()
    arc = archive_mod.Archive.load(args.archive)
    for problem in arc.problems:
        print(f"  ! {problem}", file=sys.stderr)
    clock = _clock(args)

    # ---- the record of what has been seen --------------------------------
    if action == "archive":
        rows = arc.summary() if not pair else [r for r in arc.summary() if r["pair"] == pair]
        if not rows:
            print(f"the archive at {arc.path} holds nothing"
                  + (f" for {pair}" if pair else ""))
            return 0
        for r in rows:
            print(f"{r['pair']}: {r['records']} record(s) -- {r['quote']} quote, {r['trade']} "
                  f"trade, {r['shown']} shown, {r['outcome']} outcome; "
                  f"{r['first'][:16]} to {r['last'][:16]}"
                  + (f"; {r['model_read']} read by a model" if r["model_read"] else ""))
        if pair and args.kind:
            print()
            for o in arc.query(pair=pair, kinds=args.kind)[-int(args.limit or 40):]:
                print(f"  {o.id}  {o.at[:16]}  {o.describe()}")
        return 0

    # ---- reading the folders ---------------------------------------------
    if action in ("ingest", "watch"):
        folders = _agent_folders(args)
        if not folders:
            print("error: nothing to read; give --chats and/or --sdr a folder",
                  file=sys.stderr)
            return 2
        model, model_note = _agent_model(args)
        print(model_note)
        state = ingest_mod.State.load(args.ingest_state)
        for problem in state.problems:
            print(f"  ! {problem}", file=sys.stderr)
        if args.forget:
            print(f"forgotten: {state.forget(args.forget if args.forget != '*' else '')} "
                  f"file(s) will be read again")
        book = _book(args) if args.known_pairs else None
        known = book.pairs if book is not None else None

        def report(result) -> None:
            print(result.summary())
            for f in result.files:
                print(f"  {f.line()}")
                for note in f.notes[:6]:
                    print(f"      . {note}")
                for skip in f.skipped[:8]:
                    print(f"      ! {skip}")
                if len(f.skipped) > 8:
                    print(f"      ! ... and {len(f.skipped) - 8} more not understood")
            for note in result.notes:
                print(f"  . {note}")

        if action == "ingest":
            result = ingest_mod.scan(folders, archive=arc, state=state, model=model,
                                     known_pairs=known, pair=pair,
                                     counterparty=args.counterparty, force=args.force)
            report(result)
            print(f"written: {arc.flush()} record(s) to {arc.path}")
            state.save()
            return 0
        print(f"watching every {args.every:g}s; stop with Ctrl-C")
        ingest_mod.watch(folders, archive=arc, state=state, every=args.every,
                         rounds=args.rounds, on_result=report, model=model,
                         known_pairs=known, pair=pair, counterparty=args.counterparty)
        return 0

    # ---- fetching the dissemination files --------------------------------
    if action == "fetch":
        from . import dtcc
        folders = [p for p in (args.sdr or [])]
        if not folders:
            print("error: say where to put them with --sdr DIR", file=sys.stderr)
            return 2
        today = clock.now.date()
        if args.days:
            days = dtcc.recent_days(int(args.days), today=today)
        else:
            try:
                start = (datetime.strptime(args.since, "%Y-%m-%d").date() if args.since
                         else today - timedelta(days=1))
                end = (datetime.strptime(args.until, "%Y-%m-%d").date() if args.until
                       else today - timedelta(days=1))
            except ValueError as exc:
                print(f"error: a date must be YYYY-MM-DD ({exc})", file=sys.stderr)
                return 2
            days = dtcc.business_days(start, end)
        proxy = args.proxy or dtcc.default_proxy()
        down = dtcc.Downloader(jurisdiction=args.jurisdiction, asset_class=args.asset,
                               report=args.report, proxy=proxy, timeout=args.timeout)
        print(f"{len(days)} date(s) into {folders[0]}"
              + (f" through {proxy}" if proxy else "")
              + f"  ({down.base}/{args.report}/{args.jurisdiction})")
        result = down.fetch(days, folders[0], today=today, overwrite=args.force,
                            on_day=lambda row: print(f"  {row.line()}"))
        for note in result.notes:
            print(f"  . {note}")
        print(result.summary())
        if not args.no_ingest and result.written:
            print("\nreading what was fetched:")
            state = ingest_mod.State.load(args.ingest_state)
            book = _book(args) if args.known_pairs else None
            scan = ingest_mod.scan([(folders[0], "sdr")], archive=arc, state=state,
                                   known_pairs=(book.pairs if book is not None else None))
            for f in scan.files:
                print(f"  {f.line()}")
            print(f"  written: {arc.flush()} record(s) to {arc.path}")
            state.save()
        return 1 if result.failed and not result.written else 0

    if not pair:
        print(f"error: '{action}' needs a pair", file=sys.stderr)
        return 2

    # ---- what the archive says -------------------------------------------
    if action == "evidence":
        syn = synthesis_mod.synthesize(
            arc, pair, asof=clock.now, half_life=args.half_life,
            min_effective=args.min_evidence, lookback_days=args.lookback,
            include_model_read=not args.no_model_read)
        for line in syn.lines():
            print(line)
        return 0

    if action == "learn":
        bank = KnowledgeBank.load(args.knowledge)
        syn = synthesis_mod.synthesize(
            arc, pair, asof=clock.now, half_life=args.half_life,
            min_effective=args.min_evidence, lookback_days=args.lookback,
            include_model_read=not args.no_model_read)
        rules = syn.proposed_rules()
        if not rules:
            print(f"{pair}: nothing in the archive has enough behind it to propose a width. "
                  f"'volkit agent evidence {pair}' shows what there is")
            return 0
        for r in rules:
            print(f"  {r.describe()}")
            print(f"      {r.text}")
        if not args.save:
            print("\n  nothing was written; add --save to put these into the bank")
            return 0
        merged, notes = merge_rules(list(bank.for_pair(pair).rules), rules)
        problems = bank.set_pair(pair, merged, clock.now,
                                 f"learned from {syn.counted} archived observation(s)")
        for note in notes:
            print(f"  . {note}")
        for problem in problems:
            print(f"  ! {problem}", file=sys.stderr)
        print(f"saved: {bank.save(args.knowledge)}")
        return 0

    # ---- what printed, in volatility terms -------------------------------
    if action == "trades":
        hist_pair = None
        if args.history:
            from .history import load_history
            history = load_history(args.history)
            hist_pair = history.pairs.get(pair)
            for problem in history.problems[:5]:
                print(f"  ! {problem}", file=sys.stderr)
        rows = arc.query(pair=pair, kinds="trade",
                         since=clock.now - timedelta(days=args.lookback), until=clock.now)
        print(f"{pair}: {len(rows)} printed trade(s) in the last {args.lookback:g} days")
        if not args.invert:
            for o in rows[-int(args.limit or 40):]:
                print(f"  {o.at[:16]}  {o.describe()}")
            print("\n  add --invert to turn these into volatilities "
                  "(--history is needed for the forward)")
            return 0
        vols, notes = synthesis_mod.invert_trades(
            arc, pair, asof=clock.now, hist_pair=hist_pair, lookback_days=args.lookback,
            discount_rate=args.discount_rate)
        for v in vols[-int(args.limit or 40):]:
            print(f"  {v.at[:16]}  {v.describe()}")
            print(f"       {v.why}")
        for note in notes:
            print(f"  . {note}")
        return 0

    # ---- a price we made, recorded by hand -------------------------------
    if action == "shown":
        from .quotes import QuoteError, parse_quotes
        text = sys.stdin.read() if args.file in (None, "-") else paths.read_text(args.file)
        try:
            run_ = parse_quotes(text, pair=pair, fly_convention=args.fly)
        except QuoteError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        # Read by the same grammar a broker run is read by, and then filed as
        # *ours*: the difference between a market somebody showed and a price
        # we made is not in the text, it is in who made it, so it is said here
        # and not inferred from the line.
        written = []
        for obs in archive_mod.from_quotes(run_, pair=pair, source="desk", origin="typed",
                                           counterparty=args.counterparty, via="hand",
                                           default_time=clock.now):
            ours = archive_mod.replace(obs, kind="shown")
            ok, why = arc.add(ours)
            if ok:
                written.append(ours)
            else:
                print(f"  ! {ours.describe()}: {why}", file=sys.stderr)
        arc.flush()
        for o in written:
            print(f"{o.id}  {o.describe()}")
        print(f"recorded: {len(written)} price(s); answer one with "
              f"'volkit agent outcome {pair} --ref <id> --result traded_ask'")
        return 0

    if action == "outcome":
        if not args.ref or not args.result:
            print("error: an outcome needs --ref (the id of the price shown) and --result",
                  file=sys.stderr)
            return 2
        target = arc.by_id(args.ref)
        if target is None:
            print(f"error: no record in the archive has the id {args.ref}. "
                  f"'volkit agent archive {pair} --kind shown' lists them", file=sys.stderr)
            return 2
        if target.kind != "shown":
            print(f"error: {args.ref} is a {target.kind} record, not a price we showed; an "
                  f"outcome answers a price we made", file=sys.stderr)
            return 2
        obs = archive_mod.outcome(target, args.result, away_level=args.away, at=clock.now,
                                  counterparty=args.counterparty)
        ok, why = arc.add(obs)
        if not ok:
            print(f"error: {why}", file=sys.stderr)
            return 2
        arc.flush()
        print(f"{obs.id}  {obs.describe()}  (answers {target.describe()})")
        return 0

    # ---- the price -------------------------------------------------------
    if action != "quote":
        print(f"error: unknown action {action!r}", file=sys.stderr)
        return 2

    text = sys.stdin.read() if args.file in (None, "-") else paths.read_text(args.file)
    if not text.strip():
        print("error: nothing to price; give one instrument a line, e.g. '1M ATM in 100mm vega'",
              file=sys.stderr)
        return 2

    model, model_note = _agent_model(args)
    book = _book(args, [pair])
    _apply_band(args, book)
    bank = KnowledgeBank.load(args.knowledge)
    for problem in bank.problems:
        print(f"  ! {problem}", file=sys.stderr)
    hist = None
    if args.history:
        from .history import load_history
        hist = load_history(args.history, book.pairs)
        for problem in hist.problems:
            print(f"  ! {problem}", file=sys.stderr)

    request = agent_mod.Request(
        pair=pair, text=text, cut=args.cut, method=args.method,
        fly_convention=args.fly, use_archive_width=not args.no_archive_width,
        half_life=args.half_life, min_effective=args.min_evidence,
        lookback_days=args.lookback, include_model_read=not args.no_model_read,
        fair_weight=args.fair_weight, axe_weight=args.axe_weight,
        skew_cap=args.skew_cap, horizon_days=args.horizon,
        vega_text=(paths.read_text(args.vega) if args.vega else ""),
        vega_scale=args.axe_scale, fallback_spread=args.fallback_spread,
        stale_days=args.stale_days, narrate=not args.no_narration)
    try:
        out = agent_mod.run(request, book=book, archive=arc, bank=bank, hist=hist, model=model)
    except agent_mod.AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(_json.dumps(out.to_json(), indent=1, sort_keys=True))
    else:
        print(model_note)
        print(out.text())
    if args.record:
        written = agent_mod.record_shown(arc, out, counterparty=args.counterparty,
                                         at=clock.now)
        arc.flush()
        print(f"\nrecorded {len(written)} price(s) in {arc.path}:")
        for o in written:
            print(f"  {o.id}  {o.describe()}")
    unpriced = [d for d in out.decisions if not d.priced]
    return 1 if unpriced and len(unpriced) == len(out.decisions) else 0


def cmd_mark(args) -> int:
    """The marking agent: how to run the fit, and what this desk does after it.

    Every action goes through the same functions a screen would call, per
    §10's rule.  ``propose`` and ``confer`` write nothing to the book and
    nothing to the workbook; ``record`` is the only one that writes at all,
    and it writes to the journal.
    """
    import json as _json
    from . import consult as consult_mod
    from . import marking as marking_mod
    from . import remarks as remarks_mod
    from . import session as session_mod

    action = args.action
    pair = (args.pair or "").upper()
    journal = remarks_mod.Journal.load(args.journal)
    for problem in journal.problems:
        print(f"  ! {problem}", file=sys.stderr)
    clock = _clock(args)

    if action == "journal":
        rows = journal.summary() if not pair else [r for r in journal.summary()
                                                   if r["pair"] == pair]
        if not rows:
            print(f"the journal at {journal.path} holds nothing"
                  + (f" for {pair}" if pair else ""))
            return 0
        for r in rows:
            print(f"{r['pair']}: {r['instances']} instance(s) -- {r['answered']} answered "
                  f"a proposal ({r['accepted']} accepted, {r['edited']} edited, "
                  f"{r['rejected']} rejected), {r['unprompted']} unprompted; "
                  f"{r['first'][:16]} to {r['last'][:16]}")
        if pair:
            print()
            for e in journal.query(pair=pair)[-int(args.limit or 20):]:
                print(f"  {e.id}  {e.describe()}")
        return 0

    if not pair:
        print(f"error: '{action}' needs a pair", file=sys.stderr)
        return 2

    tendencies = marking_mod.learn(journal, pair, asof=clock.now,
                                   lookback_days=args.lookback,
                                   min_instances=args.min_instances)
    if action == "learn":
        for line in tendencies.lines():
            print(line)
        return 0

    book = _book(args, [pair])
    _apply_band(args, book)

    if action == "record":
        if not args.proposal:
            print("error: 'record' needs --proposal, the file 'propose --out' wrote",
                  file=sys.stderr)
            return 2
        try:
            saved = _json.loads(paths.read_text(args.proposal))
        except (OSError, ValueError) as exc:
            print(f"error: the proposal file could not be read: {exc}", file=sys.stderr)
            return 2
        # The book is the outcome.  A session file named here is put on it
        # first, which is how an edit made on the screen gets recorded as the
        # edit rather than as the proposal the screen started from.
        if args.session:
            session_mod.restore(book, args.session)
        try:
            entry = marking_mod.answer(
                journal, pair, before=saved.get("before") or {},
                proposed=saved.get("after") or {},
                after=session_mod.capture_pair(book, pair),
                verdict=args.verdict, note=args.note, source="cli", at=clock.now,
                context=saved.get("context") or {})
        except marking_mod.MarkingError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"{entry.id}  {entry.describe()}")
        moved = entry.changes()
        if args.verdict == "edited" and not any(c.correction for c in moved):
            print("  . nothing differs from the proposal; 'accepted' is the verdict for that")
        print(f"recorded to {journal.path}")
        return 0

    # ---- propose / confer -------------------------------------------------
    if action == "confer":
        from . import archive as archive_mod
        from . import synthesis as synthesis_mod
        arc = archive_mod.Archive.load(args.archive)
        for problem in arc.problems:
            print(f"  ! {problem}", file=sys.stderr)
        syn = synthesis_mod.synthesize(arc, pair, asof=clock.now,
                                       half_life=args.half_life,
                                       min_effective=args.min_evidence,
                                       lookback_days=args.evidence_lookback)
        conference = consult_mod.confer(book, pair, syn, tendencies=tendencies,
                                        method=args.method, cut=args.cut,
                                        rounds=args.rounds, mid_pull=args.mid_pull)
        if args.json:
            print(_json.dumps(conference.to_json(), indent=1, sort_keys=True))
        else:
            for line in conference.lines():
                print(line)
        proposal = conference.proposal
    elif args.file or args.target_source:
        # The card's own path: the same panel the browser posts, so what
        # this prints is what the market-maker tab shows.
        from . import archive as archive_mod
        arc = None
        if not args.no_archive:
            arc = archive_mod.Archive.load(args.archive)
            for problem in arc.problems:
                print(f"  ! archive: {problem}", file=sys.stderr)
        market_text = ""
        if args.file == "-":
            market_text = sys.stdin.read()
        elif args.file:
            market_text = paths.read_text(args.file)
        source = args.target_source or ("paste" if args.target else "quotes")
        panel = marking_mod.panel_from_request({
            "pair": pair, "cut": args.cut, "method": args.method, "text": market_text,
            "target_source": source,
            "target_text": paths.read_text(args.target) if args.target else "",
            "free": args.free, "smile_free": args.smile_free,
            "choose_knobs": not args.no_choose, "mid_pull": args.mid_pull,
            "lookback_days": args.lookback, "min_instances": args.min_instances,
            "use_archive": not args.no_archive, "half_life": args.half_life,
            "min_effective": args.min_evidence, "evidence_lookback": args.evidence_lookback,
        })
        out = panel.run(book, journal, archive=arc)
        proposal = None
        if args.json:
            print(_json.dumps(out, indent=1, sort_keys=True, default=str))
        else:
            print(f"{pair}: target curve -- {out['targets']['evidence']}")
            for line in out["lines"]:
                print(line)
            crit = out.get("critique")
            if crit:
                print(f"  judge  {crit['verdict']}")
                for r in crit.get("rows", []):
                    if r["improved"] or r["worsened"]:
                        print(f"         {r['line']}")
            for n in out["notes"]:
                print(f"  note   {n}")
            for w in out["warnings"]:
                print(f"  !      {w}")
        if args.out:
            paths.write_text(args.out, _json.dumps(out["proposal"], indent=1, sort_keys=True))
            print(f"\nproposal written to {args.out}")
            print(f"  accept or edit it, then: volkit mark record {pair} "
                  f"--proposal {args.out} --verdict edited")
        elif not args.json:
            print("\n  nothing has been written; --out saves this so 'mark record' can answer it")
        return 0
    else:
        targets, weights = [], []
        if args.target:
            from .marketmaker import CurveTarget
            for n, line in enumerate(paths.read_text(args.target).splitlines(), start=1):
                bits = line.split()
                if len(bits) < 2:
                    continue
                try:
                    targets.append(CurveTarget(tenor=bits[0].upper(),
                                               t=tenor_to_years(bits[0]),
                                               vol=float(bits[1]) / 100.0, source="pasted"))
                except (ValueError, KeyError) as exc:
                    print(f"  ! target line {n}: {exc}", file=sys.stderr)
        if not targets:
            print("error: 'propose' needs a target curve; give --target a file of "
                  "'tenor vol' lines, or use 'confer' to take them from the archive",
                  file=sys.stderr)
            return 2
        proposal = marking_mod.propose(book, pair, targets=targets, tendencies=tendencies,
                                       method=args.method, cut=args.cut,
                                       mid_pull=args.mid_pull, free=args.free)
        if args.json:
            print(_json.dumps(proposal.to_json(), indent=1, sort_keys=True))
        else:
            for line in proposal.lines():
                print(line)

    if proposal is None:
        return 0
    if args.out:
        paths.write_text(args.out, _json.dumps(proposal.to_json(), indent=1, sort_keys=True))
        print(f"\nproposal written to {args.out}")
        print(f"  accept or edit it, then: volkit mark record {pair} "
              f"--proposal {args.out} --verdict edited")
    elif not args.json:
        print("\n  nothing has been written; --out saves this so 'mark record' can answer it")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="volkit", description="FX volatility surface toolkit",
        epilog=screens.summary() or None,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-w", "--workbook", default=_default_workbook(), help="market data workbook")
    p.add_argument("--asof", help="valuation datetime, e.g. '2024-02-28 12:00' (default: now, UTC)")
    # Read before argparse exists -- a hidden screen has to be switched on
    # before its subcommand can be registered, and the configuration file has
    # to become arguments before there are any.  Declared here so --help
    # documents them and so they are never an "unrecognised argument".
    p.add_argument("--enable-tab", action="append", default=[], metavar="SCREEN",
                   help="turn on a screen this build hides; repeatable. "
                        + (screens.summary() or "this build hides none"))
    p.add_argument("--config", metavar="PATH",
                   help=f"startup settings file ({', '.join(config.DEFAULT_NAMES)} beside the "
                        "executable is read when volkit is started with no arguments at all)")
    p.add_argument("--no-config", action="store_true",
                   help="ignore the startup settings file")
    p.add_argument("--session", metavar="PATH",
                   help="saved marks to put on the book before anything is priced "
                        f"({session.SESSION_FILENAME} beside the workbook is the usual name). "
                        "The workbook itself is never written to")

    # The same options are attached to every subcommand as well, so they work
    # in either position.  People naturally type
    # "volkit tenors USDJPY --asof ..." rather than putting the option before
    # the subcommand, and argparse rejects that unless the subparser knows it
    # too.  SUPPRESS stops the subparser overwriting a value given earlier
    # with its own default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--workbook", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--asof", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--enable-tab", action="append", default=argparse.SUPPRESS,
                        help=argparse.SUPPRESS)
    common.add_argument("--session", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    # Everything a marker may move about a managed band.  Percentages, because
    # that is how they are read on the screen; BandTreatment.from_request does
    # the one conversion into decimals.
    band_opts = argparse.ArgumentParser(add_help=False)
    band_opts.add_argument("--band-mode", choices=list(_band_modes()),
                           help="warn (default), off, or mixture. --method BAND implies mixture")
    band_opts.add_argument("--hazard", type=float,
                           help="annual peg-break intensity in %%, e.g. 2 (default 2)")
    band_opts.add_argument("--weak-share", type=float,
                           help="share of breaks that go the weak side, in %% (default 85)")
    band_opts.add_argument("--weak-jump", type=float, help="weak-side jump size in %% (default 6)")
    band_opts.add_argument("--weak-vol", type=float,
                           help="volatility after a weak-side break, in %% (default 10)")
    band_opts.add_argument("--strong-jump", type=float,
                           help="strong-side jump size in %% (default 4)")
    band_opts.add_argument("--strong-vol", type=float,
                           help="volatility after a strong-side break, in %% (default 8)")
    band_opts.add_argument("--band-lower", type=float, help="override the band's lower edge")
    band_opts.add_argument("--band-upper", type=float, help="override the band's upper edge")
    band_opts.add_argument("--band-blend", type=float,
                           help="%% of the band model in the quoted smile (default 100). "
                                "Anything between is a marking convenience, not a model")
    band_opts.add_argument("--band-delta", type=float,
                           help="which wing the band model reports against, in delta %% "
                                "(default 25)")
    band_opts.add_argument("--solve-hazard", action="store_true",
                           help="back the hazard out of the quoted wings instead of marking it")

    sub = p.add_subparsers(dest="cmd", required=True)

    def add_command(name: str, **kw):
        """Register a subcommand unless its screen was excluded from the build.

        An excluded one is still *built*, into a parser nothing reaches, so
        each call site stays one line and the options of a trimmed build
        cannot drift from those of a full one.  ``check`` and ``serve``
        belong to no screen and are always registered.
        """
        if screens.command_enabled(name):
            return sub.add_parser(name, **kw)
        kw.pop("help", None)
        return argparse.ArgumentParser(prog=name, add_help=False, **kw)

    s = add_command("check", parents=[common], help="validate the workbook without building anything")
    s.set_defaults(func=cmd_check)

    s = add_command("session", parents=[common],
                    help="save the marks on the book to a file, or put a saved file back")
    s.add_argument("path", nargs="?",
                   help=f"the session file (default: {session.SESSION_FILENAME} beside the "
                        "workbook)")
    s.add_argument("--load", action="store_true",
                   help="put the file's marks on the book instead of writing it")
    s.add_argument("--show", action="store_true",
                   help="print what a file holds without loading a workbook")
    s.add_argument("--pair", help="one pair only (default: every pair in the book)")
    s.add_argument("--note", default="", help="a line recorded in the file")
    s.set_defaults(func=cmd_session)

    s = add_command("serve", parents=[common], help="run the local web interface")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV (pair,tenor,value)")
    s.add_argument("--history", default=_default_history(),
                   help="historical workbook for the analysis screen")
    s.add_argument("--knowledge", help="knowledge bank JSON (default: beside the workbook)")
    s.add_argument("--auto-reload", type=float, nargs="?", const=5.0, default=0.0,
                   metavar="SECONDS",
                   help="watch the market feed and re-read it when it is written (default: "
                        "off; bare flag = every 5s). Only the feed: the workbook and the "
                        "historical sheet stay on their buttons, because reloading the "
                        "workbook discards this session's marks. The pricing screen has the "
                        "same switch")
    # The desk agent's card lives inside the market-maker tab, and its folders
    # are named here rather than typed into the page: a path a browser can
    # post is a path anything that reaches the browser can read.
    s.add_argument("--archive", help=f"the observation archive the desk-agent card reads "
                                     f"(default: {archive.ARCHIVE_FILENAME} beside the workbook)")
    s.add_argument("--chats", action="append", default=[], metavar="DIR",
                   help="a folder of broker chats the agent card may scan; repeatable")
    s.add_argument("--sdr", action="append", default=[], metavar="DIR",
                   help="a folder of SDR dissemination files it may scan; repeatable")
    s.add_argument("--ingest-state", help="where the record of what has been read lives")
    s.add_argument("--proxy", help="HTTP proxy for the DTCC download (default: the one the "
                                   "environment names in https_proxy / HTTP_PROXY)")
    s.add_argument("--journal", help=f"the re-marking journal the marking-agent card reads "
                                     f"and writes (default: {remarks.JOURNAL_FILENAME} beside "
                                     f"the workbook)")
    s.set_defaults(func=cmd_serve)

    s = add_command("mm", parents=[common],
                       help="fit a curve and the wings to a market, and quote what is asked for")
    s.add_argument("pair")
    s.add_argument("--file", help="the market: a pasted broker run, what the fit is aimed at "
                                  "(default: stdin, when --request is not given)")
    s.add_argument("--request", help="the options to be quoted, one an instrument a line and no "
                                     "prices on them: '1M ATM in 100mm', '3M 25d RR'. Priced off "
                                     "the fit above when there is one, and off the marks as they "
                                     "stand when there is not")
    s.add_argument("--target-source", default="overwrites", choices=list(_target_sources()),
                   help="where the target at-the-money curve comes from")
    s.add_argument("--target", help="file of 'tenor vol' lines, for --target-source paste")
    s.add_argument("--free", nargs="*", help="curve parameters to leave free")
    s.add_argument("--smile-free", nargs="*", help="smile parameters to leave free")
    s.add_argument("--no-curve", action="store_true", help="do not fit the at-the-money curve")
    s.add_argument("--no-wings", action="store_true", help="do not fine tune the wings")
    s.add_argument("--mid-pull", type=float, default=0.05,
                   help="weight of the pull toward the quoted mids inside the hinge")
    s.add_argument("--max-evals", type=int, default=300, help="fine-tune evaluation budget")
    s.add_argument("--vega", help="file of 'tenor vega' lines: the position leaning the mid")
    s.add_argument("--axe-scale", type=float, default=0.0,
                   help="the position that counts as a full axe, in the profile's own unit")
    s.add_argument("--fair-weight", type=float, default=0.25,
                   help="how much of the fair-value richness leans the mid")
    s.add_argument("--axe-weight", type=float, default=0.5,
                   help="how much of a half width a full axe leans the mid")
    s.add_argument("--skew-cap", type=float, default=1.0,
                   help="cap on the total lean, as a multiple of the half width")
    s.add_argument("--horizon", type=float, default=30.0, help="fair value horizon in days")
    s.add_argument("--fallback-spread", type=float,
                   help="width in vol points for quotes no bank rule matches")
    s.add_argument("--archive-width", action="store_true",
                   help="put the observation archive on the width ladder, between the bank "
                        "and the fallback: a quote no rule matches is shown at the width the "
                        "market has shown it at, when the archive holds enough")
    s.add_argument("--archive", help=f"the observation archive, with --archive-width "
                                     f"(default: {archive.ARCHIVE_FILENAME} beside the workbook)")
    s.add_argument("--knowledge", help="knowledge bank JSON (default: beside the workbook)")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV. Needed for a quote written against an "
                        "absolute strike, which has to become a moneyness before the surface "
                        "can be read at it")
    s.add_argument("--history", default=_default_history(),
                   help="historical workbook, for the fair-value lean")
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--fly", default="market", choices=["market", "smile"],
                   help="which butterfly an unqualified 'fly' quote means")
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.add_argument("--learn", action="store_true",
                   help="propose bank rules from the pasted widths instead of quoting")
    s.add_argument("--save", action="store_true", help="with --learn, write them to the bank")
    s.set_defaults(func=cmd_mm)

    # The desk agent.  One command with an action on it rather than a family
    # of commands: every action shares the archive, the bank and the model
    # options, and splitting them would mean repeating that list five times
    # and having it drift four ways.
    s = add_command("agent", parents=[common],
                    help="keep an archive of what the market has shown, and quote from it")
    s.add_argument("action", choices=("quote", "ingest", "watch", "fetch", "trades",
                                      "evidence", "learn", "shown", "outcome", "archive"),
                   help="quote: make a price. fetch: download DTCC's public dissemination "
                        "files. ingest/watch: read the folders. trades: what printed, and "
                        "what volatility it implies. evidence: what the archive says. learn: "
                        "propose bank rules from it. shown/outcome: record a price and what "
                        "became of it. archive: what is held")
    s.add_argument("pair", nargs="?", help="the currency pair")
    s.add_argument("--file", default="-",
                   help="what to price, one instrument a line (default: stdin)")
    s.add_argument("--archive", help=f"the observation archive "
                                     f"(default: {archive.ARCHIVE_FILENAME} beside the workbook)")
    s.add_argument("--chats", action="append", default=[], metavar="DIR",
                   help="a folder of broker chats to read; repeatable")
    s.add_argument("--sdr", action="append", default=[], metavar="DIR",
                   help="a folder of SDR dissemination files; repeatable")
    s.add_argument("--ingest-state", help="where the record of what has been read lives")
    s.add_argument("--force", action="store_true", help="read every watched file again")
    s.add_argument("--forget", metavar="PATTERN",
                   help="drop what has been read for paths containing PATTERN ('*' for all)")
    s.add_argument("--known-pairs", action="store_true",
                   help="load the workbook first and keep only pairs it builds")
    s.add_argument("--every", type=float, default=30.0, help="watch interval in seconds")
    s.add_argument("--rounds", type=int, default=0, help="stop after this many passes (0: never)")
    s.add_argument("--counterparty", default="", help="who the quotes or the price are with")
    s.add_argument("--kind", choices=archive.KINDS, help="with 'archive', list records of a kind")
    s.add_argument("--limit", type=int, default=40, help="how many records to list")
    s.add_argument("--ref", help="with 'outcome', the id of the price being answered")
    s.add_argument("--result", choices=archive.RESULTS, help="with 'outcome', what happened")
    s.add_argument("--away", type=float,
                   help="with 'outcome --result done_away', the level it traded at")
    # the archive
    s.add_argument("--half-life", type=float, default=synthesis.DEFAULT_HALF_LIFE,
                   help="days after which an observation counts half (default 5)")
    s.add_argument("--min-evidence", type=float, default=synthesis.DEFAULT_MIN_EFFECTIVE,
                   help="age-weighted observations needed before a width is produced")
    s.add_argument("--lookback", type=float, default=90.0,
                   help="how far back the archive is read, in days")
    s.add_argument("--no-model-read", action="store_true",
                   help="leave observations a language model transcribed out of the figures")
    s.add_argument("--no-archive-width", action="store_true",
                   help="widths from the bank only; the archive is shown and not used")
    s.add_argument("--stale-days", type=float, default=5.0,
                   help="an archived level older than this is called stale on the row")
    # the model
    s.add_argument("--llm-backend", choices=list(llm.BACKENDS),
                   help="ollama (default) or an OpenAI-compatible endpoint")
    s.add_argument("--llm-url", help="where it listens, e.g. http://127.0.0.1:11434")
    s.add_argument("--llm-model", help="the model name to ask for")
    s.add_argument("--no-llm", action="store_true", help="do not use a local model at all")
    s.add_argument("--no-narration", action="store_true",
                   help="skip the written explanation; the itemised one is always there")
    # the leans, named as the market-maker screen names them
    s.add_argument("--fair-weight", type=float, default=0.25,
                   help="how much of the fair-value richness leans the mid")
    s.add_argument("--axe-weight", type=float, default=0.5,
                   help="how much of a half width a full axe leans the mid")
    s.add_argument("--skew-cap", type=float, default=1.0,
                   help="cap on the total lean, as a multiple of the half width")
    s.add_argument("--horizon", type=float, default=30.0, help="fair value horizon in days")
    s.add_argument("--vega", help="file of 'tenor vega' lines: the position leaning the mid")
    s.add_argument("--axe-scale", type=float, default=0.0,
                   help="the position that counts as a full axe, in the profile's own unit")
    s.add_argument("--fallback-spread", type=float,
                   help="width for quotes no bank rule and no archive evidence covers")
    s.add_argument("--knowledge", help="knowledge bank JSON (default: beside the workbook)")
    s.add_argument("--history", default=_default_history(),
                   help="historical workbook, for the fair-value lean")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV, needed for an absolute strike")
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default=None, help="default: the pair's marked method")
    s.add_argument("--fly", default="market", choices=("market", "smile"),
                   help="which butterfly an unqualified 'fly' is")
    s.add_argument("--record", action="store_true",
                   help="write the prices this run made into the archive, so they can be "
                        "answered later with 'agent outcome'")
    # fetching from DTCC
    s.add_argument("--since", metavar="YYYY-MM-DD", help="with 'fetch', the first date")
    s.add_argument("--until", metavar="YYYY-MM-DD", help="with 'fetch', the last date")
    s.add_argument("--days", type=int, help="with 'fetch', the last N business days instead")
    s.add_argument("--jurisdiction", default="cftc", choices=list(dtcc.JURISDICTIONS))
    s.add_argument("--asset", default="FOREX", choices=list(dtcc.ASSET_CLASSES))
    s.add_argument("--report", default="cumulative", choices=list(dtcc.REPORTS),
                   help="cumulative: a whole day, published after it. slice: intraday")
    s.add_argument("--proxy", help="HTTP proxy for the download (default: the one the "
                                   "environment names in https_proxy / HTTP_PROXY)")
    s.add_argument("--timeout", type=float, default=120.0, help="seconds per request")
    s.add_argument("--no-ingest", action="store_true",
                   help="with 'fetch', download only and do not read what arrived")
    # what printed
    s.add_argument("--invert", action="store_true",
                   help="with 'trades', turn each premium into a volatility. Needs --history "
                        "for the forward on the trade's own date; a trade whose forward "
                        "cannot be found is refused rather than priced off today's")
    s.add_argument("--discount-rate", type=float,
                   help="with 'trades --invert', a continuously compounded rate to discount "
                        "at. This package carries no rate curve, so without one the premium "
                        "is inverted undiscounted and the volatility reads a touch low")
    s.add_argument("--save", action="store_true",
                   help="with 'learn', write the proposed rules into the knowledge bank. "
                        "Proposing and saving are two steps here for the same reason they are "
                        "on the market-maker screen: a width nobody approved is a width "
                        "nobody can be asked about")
    s.add_argument("--json", action="store_true", help="the whole decision as JSON")
    s.set_defaults(func=cmd_agent)

    # The marking agent.  Owned by the marking screen: it decides how to run
    # that screen's fit, and a build without the tab has nothing for it to do.
    s = add_command("mark", parents=[common],
                    help="the marking agent: plan the fit, learn what this desk does after it")
    s.add_argument("action", choices=("propose", "confer", "learn", "journal", "record"),
                   help="propose: plan and run the fit against a target curve. confer: take "
                        "the targets from the quote archive and let the two agents settle "
                        "on a re-mark. learn: what the journal says about this desk. "
                        "journal: what is held. record: answer a proposal")
    s.add_argument("pair", nargs="?")
    s.add_argument("--journal", help=f"the re-marking journal "
                                     f"(default: {remarks.JOURNAL_FILENAME} beside the workbook)")
    s.add_argument("--target", help="with 'propose', a file of 'tenor vol' lines")
    s.add_argument("--file", help="with 'propose', the market: a pasted broker run, read "
                                  "exactly as 'mm' reads it. With this the proposal is the "
                                  "market-maker card's -- the wings are tuned to the run "
                                  "and the target curve comes from --target-source")
    s.add_argument("--target-source", default=None, choices=list(_target_sources()),
                   help="with --file, where the target at-the-money curve comes from "
                        "(default: 'paste' when --target is given, else 'quotes')")
    s.add_argument("--no-choose", action="store_true",
                   help="with --file, free exactly --free and --smile-free rather than "
                        "letting the agent choose the knobs")
    s.add_argument("--smile-free", nargs="*", help="smile parameters, with --no-choose")
    s.add_argument("--no-archive", action="store_true",
                   help="with --file, do not score the proposal against the archive")
    s.add_argument("--free", nargs="*", help="curve parameters to leave free, overriding "
                                             "both the default set and anything learned")
    s.add_argument("--out", metavar="PATH", help="write the proposal, so 'record' can answer it")
    s.add_argument("--proposal", metavar="PATH", help="with 'record', the file --out wrote")
    s.add_argument("--verdict", default="edited", choices=list(remarks.VERDICTS),
                   help="with 'record', what you did to the proposal")
    s.add_argument("--note", default="", help="with 'record', a line kept beside the instance")
    s.add_argument("--limit", type=int, default=20, help="how many instances to list")
    # what it learns from
    s.add_argument("--lookback", type=float, default=365.0,
                   help="how far back the journal is read, in days. Age is not a weight "
                        "here: how a desk marks is a fact about the desk")
    s.add_argument("--min-instances", type=int, default=marking.MIN_INSTANCES,
                   help="instances behind a knob before any tendency about it is stated")
    # the exchange
    s.add_argument("--archive", help="with 'confer', the observation archive the quoting "
                                     "agent reads")
    s.add_argument("--rounds", type=int, default=consult.MAX_ROUNDS,
                   help="with 'confer', propose-and-critique rounds before a person decides")
    s.add_argument("--half-life", type=float, default=synthesis.DEFAULT_HALF_LIFE,
                   help="days after which an archived quote counts half")
    s.add_argument("--min-evidence", type=float, default=synthesis.DEFAULT_MIN_EFFECTIVE)
    s.add_argument("--evidence-lookback", type=float, default=90.0)
    s.add_argument("--mid-pull", type=float, default=0.05,
                   help="weight of the pull toward the quoted mids inside the hinge")
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default=None, help="default: the pair's marked method")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_mark)

    s = add_command("validate", parents=[common], help="hunt for competing smile calibrations")
    s.add_argument("pair", nargs="?", help="default: every pair")
    s.set_defaults(func=cmd_validate)

    s = add_command("events", parents=[common], help="scheduled economic events for a pair")
    s.add_argument("pair")
    s.add_argument("--horizon", type=float, default=1.0, help="years")
    s.set_defaults(func=cmd_events)

    s = add_command("tenors", parents=[common], help="print the ATM term structure")
    s.add_argument("pair")
    s.add_argument("--cut", default="TK")
    s.set_defaults(func=cmd_tenors)

    s = add_command("daily", parents=[common], help="export the daily volatility series")
    s.add_argument("pair")
    s.add_argument("--horizon", type=float, default=1.0, help="years")
    s.add_argument("--cut", default="NY")
    s.add_argument("--field", default="cumulative", choices=["cumulative", "daily"])
    s.add_argument("--out", help="output file (default: stdout)")
    s.set_defaults(func=cmd_daily)

    s = add_command("band", parents=[common, band_opts],
                       help="the managed-band read-out for a pegged pair")
    s.add_argument("pair")
    s.add_argument("--tenor", help="one tenor (default: every quoted tenor)")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV; a band is absolute, so placing it "
                        "against the surface needs an outright forward")
    s.add_argument("--cut", default="NY")
    s.set_defaults(func=cmd_band)

    s = add_command("vol", parents=[common, band_opts],
                       help="volatility for a strike and expiry")
    s.add_argument("pair")
    s.add_argument("expiry", help="a tenor (1W, 8d, 3M) or a date (2024-05-28, "
                                  "28May24, 5/28/2024)")
    s.add_argument("--strike", type=float)
    s.add_argument("--forward", type=float, default=1.0)
    s.add_argument("--method", default="SVI", choices=list(_methods()))
    s.add_argument("--cut", default="TK")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV, for placing a managed band")
    s.set_defaults(func=cmd_vol)

    s = add_command("analysis", parents=[common],
                       help="carry and roll, realized vs implied, fair value, cross triangle")
    s.add_argument("pair")
    s.add_argument("--history", help="historical workbook: one sheet per pair, one row per date")
    s.add_argument("--feed", default=_default_feed(), help="spot / forward feed CSV")
    s.add_argument("--horizon", type=float, default=30.0, help="roll horizon in days")
    s.add_argument("--lookback", default="match",
                   help="realized window in days, or 'match' to use each tenor's own length")
    s.add_argument("--target", default="atm", choices=sorted(_targets()),
                   help="which point on the smile to roll")
    s.add_argument("--annualisation", default="weighted", choices=["weighted", "calendar", "count"])
    s.add_argument("--realized-basis", default="auto", choices=["auto", "spot", "forward"],
                   help="measure realized volatility on the forward to each tenor (the "
                        "like-for-like comparison, and the default where the sheet quotes "
                        "swap points) or on spot alone")
    s.add_argument("--sabr", action="store_true",
                   help="also read the risk reversal and the butterfly as a spot/volatility "
                        "correlation and a vol of vol, against the same two measured from history")
    s.add_argument("--sabr-delta", type=float, default=0.25,
                   help="which wings to read the SABR shape off (default 0.25)")
    s.add_argument("--relative-value", action="store_true",
                   help="also score every expiry and strike for relative value: implied "
                        "against realized in level and in shape, the roll and the forward "
                        "carry, the cross triangle, and each cell's own z-score in history")
    s.add_argument("--history-days", type=float, default=_history_days(),
                   help="how far back 'recent history' reaches for the z-scores and for the "
                        "scale they are measured in. Deliberately not the realized lookback, "
                        "which is matched to each tenor")
    s.add_argument("--weight", action="append", default=[], metavar="SIGNAL=W",
                   help="reweight one relative-value signal, e.g. --weight carry=0.4. "
                        "Weights are renormalised over whichever signals a cell has")
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.add_argument("--no-noise", action="store_true",
                   help="skip the triangle's reconstruction diagnostic (faster)")
    # Kept on this command, and refused with an address rather than dropped:
    # the panel moved to the Monitor screen and argparse's "unrecognised
    # argument" would not say where it went.
    s.add_argument("--compare", action="append", default=[], metavar="SOURCE",
                   help="moved to 'volkit monitor --compare'; kept here so the old command "
                        "line says where the curve comparison went")
    s.add_argument("--field", default="atm", choices=list(_curve_fields()),
                   help="which quoted number a moved --compare would have shown")
    s.set_defaults(func=cmd_analysis)

    s = add_command("monitor", parents=[common],
                    help="what has moved: small panels per pair, and the curve comparison")
    s.add_argument("pair", nargs="?",
                   help="the default pair for --watch and --compare")
    s.add_argument("--watch", action="append", default=[], metavar="SPEC",
                   help="one tile: PAIR[:was[:now]], where each end is a source with an "
                        "optional date -- EURUSD, USDJPY:history@-1m, "
                        "EURJPY:history@-1w:history@latest. Repeatable; the default is the "
                        "pair's fitted surface against its historical row a week ago")
    s.add_argument("--compare", action="append", default=[], metavar="SOURCE",
                   help="also print the curve comparison: surface, marks, history, "
                        "history:-30d, history:2024-01-15:EURUSD. Repeatable; the first is "
                        "the base everything else is differenced against")
    s.add_argument("--field", default="atm", choices=list(_curve_fields()),
                   help="which of the five numbers the levels column shows, and what the "
                        "comparison is differenced on")
    s.add_argument("--history", default=_default_history(),
                   help="historical workbook: one sheet per pair, one row per date")
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.set_defaults(func=cmd_monitor)

    s = add_command("listed", parents=[common],
                       help="fit SABR to an exchange-traded strike/vol table")
    s.add_argument("underlying",
                   help="contract code. The ones this build knows (6E, 6J, ...) bring their "
                        "pair, strike direction and contract size with them; any other code "
                        "is taken as typed and takes those from --pair / --invert / "
                        "--contract-size")
    s.add_argument("--expiry", required=True, help="e.g. '2026-09-11 19:00' (the exchange's own cut)")
    s.add_argument("--forward", type=float, required=True, help="futures price in the listed units")
    s.add_argument("--file", default="-", help="strike/vol table (default: stdin)")
    s.add_argument("--pair", help="override the FX pair this contract maps to")
    s.add_argument("--invert", type=lambda v: v.lower() in ("1", "true", "yes", "on"),
                   default=None, help="override whether listed strikes are the reciprocal of the pair")
    s.add_argument("--scale", type=float, help="multiply quoted strikes before mapping, e.g. 1e-6")
    s.add_argument("--beta", type=float, default=1.0, help="SABR beta (default 1, lognormal)")
    # The three the fit moves.  Giving one holds it there and fits the rest
    # around it; give all three and nothing is fitted, the quotes are simply
    # priced against the curve.
    s.add_argument("--alpha", type=float, help="hold SABR alpha at this value instead of fitting it")
    s.add_argument("--rho", type=float, help="hold SABR rho at this value instead of fitting it")
    s.add_argument("--nu", type=float, help="hold SABR nu (volvol) at this value instead of fitting it")
    s.add_argument("--weighting", default="vega", choices=["vega", "equal", "table"])
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--strike-column", type=int, help="1-based, overrides header detection")
    s.add_argument("--vol-column", type=int, help="1-based, overrides header detection")
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.add_argument("--no-compare", action="store_true", help="fit only; do not load the book")
    s.add_argument("--positions", metavar="PATH",
                   help="a position table to price against this panel and aggregate: "
                        "'contract, expiry, strike, C/P, contracts', or just "
                        "'strike, C/P, contracts' when there is one panel to answer. Greeks "
                        "are reported both Black-Scholes and on the fitted smile")
    s.add_argument("--panels", metavar="PATH",
                   help="a JSON list of further fit panels to price --positions against, in "
                        "the shape the Exchange-traded screen posts. This is what reproduces "
                        "a screen holding several contracts; the panel built from the "
                        "arguments above is the first of them")
    s.add_argument("--contract-size", type=float,
                   help="units of the base currency one option covers (default: the "
                        "contract's own, e.g. 125000 for 6E). Only the position greeks use it")
    s.add_argument("--vol-bump", type=float, default=1.0, metavar="POINTS",
                   help="vega, vanna and volga are per this many volatility points (default 1)")
    s.add_argument("--theta-days", type=float, default=1.0, metavar="DAYS",
                   help="theta is the decay over this many days (default 1)")
    s.set_defaults(func=cmd_listed)

    s = add_command("smile", parents=[common, band_opts],
                       help="print the smile at one expiry")
    s.add_argument("pair")
    s.add_argument("expiry", help="a tenor (1W, 8d, 3M) or a date (2024-05-28, "
                                  "28May24, 5/28/2024)")
    s.add_argument("--method", default="SVI", choices=list(_methods()))
    s.add_argument("--cut", default="TK")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV, for placing a managed band")
    s.set_defaults(func=cmd_smile)
    return p


def _excluded_request(argv: list[str]) -> str | None:
    """The screen behind an excluded subcommand the user has just typed.

    argparse would answer 'invalid choice', which in a trimmed build is a lie:
    the command exists, this copy was built without it.  Only the first
    positional token is inspected -- the options that may precede it are the
    two global ones, and everything after it belongs to the subcommand.
    """
    takes_value = {"-w", "--workbook", "--asof", "--enable-tab", "--config", "--session"}
    skip = False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok.startswith("-"):
            skip = tok in takes_value
            continue
        owner = screens.command_screen(tok)
        return owner if owner and not screens.is_enabled(owner) else None
    return None


def _requested_screens(argv: list[str]) -> list[str]:
    """The ``--enable-tab`` names in an argument list.

    Read off argv rather than off the parsed arguments because the parser
    cannot be built until this has been answered: a hidden screen's
    subcommands are not registered, so ``volkit --enable-tab mm mm EURUSD``
    would be rejected by the parser that the flag is meant to change.
    """
    out: list[str] = []
    for i, tok in enumerate(argv):
        if tok.startswith("--enable-tab="):
            out.append(tok.split("=", 1)[1])
        elif tok == "--enable-tab" and i + 1 < len(argv):
            out.append(argv[i + 1])
    return out


def _utf8_streams() -> None:
    """Speak UTF-8 on the standard streams, whatever the machine's locale is.

    The tables printed here are full of em dashes and greek letters, and a
    tile's label carries an arrow.  Interactively Windows writes them through
    the console's own Unicode call and all is well, but the moment the output
    is piped or redirected Python falls back to the locale encoding -- cp1252
    on the desk -- and a single arrow ends the run with a UnicodeEncodeError
    instead of a table.  A redirected *input* is the same story: a broker run
    saved out of a chat window is UTF-8, and read as cp1252 the quotes come
    back as mojibake.  ``errors="replace"`` on the way in, because a byte this
    tool cannot read is one bad character on one line and is visible there,
    not a reason to refuse a whole run.
    """
    for stream, errors in ((sys.stdin, "replace"), (sys.stdout, "strict"),
                           (sys.stderr, "strict")):
        try:
            stream.reconfigure(encoding="utf-8", errors=errors)
        except (AttributeError, ValueError, OSError):
            pass          # not a real stream (a test's StringIO, or a pipe
                          # somebody has already wrapped); nothing to do.


def main(argv=None) -> int:
    from . import preflight

    _utf8_streams()
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        # The startup file becomes arguments before anything else looks at
        # them, and says so: a packaged app quietly taking its orders from a
        # file nobody remembers writing is the invisible behaviour this
        # project exists to remove.
        raw, cfg = config.startup_argv(raw, KNOWN_COMMANDS)
        if cfg.path is not None:
            print(f"settings from {cfg.path}")
            for note in cfg.notes:
                print(f"  {note}")
        wanted = _requested_screens(raw)
        if wanted:
            turned = screens.activate(wanted)
            for name in turned:
                print(f"screen enabled: {screens.BY_NAME[name].label}")
    except (config.ConfigError, screens.ScreenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    problems = preflight.run()
    if any("not installed" in p or "time zone database" in p for p in problems):
        return 3
    gone = _excluded_request(raw)
    if gone is not None:
        print(f"error: {screens.excluded_message(gone)}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(raw)
    try:
        return args.func(args)
    except (MarketDataError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
