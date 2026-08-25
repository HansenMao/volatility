"""Command line interface.

The legacy tool could only be driven through its Tkinter window, so nothing
was scriptable and nothing could run in a batch job.  Every operation the GUI
offered is available here too, plus a ``check`` command that validates a
workbook without building anything.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import config, screens
from .book import Book
from .marketdata import ExcelSource, MarketDataError
from .timeutil import UTC, Clock, parse_datetime, tenor_to_years

#: Every subcommand name, whether or not this build registers it.  Used before
#: the parser exists, to tell a configuration file's command from its options.
KNOWN_COMMANDS = frozenset(screens.all_commands()) | {"check", "serve"}

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
        Path(args.out).write_text("\n".join(lines) + "\n")
        print(f"wrote {len(lines)} rows to {args.out}")
    else:
        print("\n".join(lines))
    return 0


def cmd_vol(args) -> int:
    book = _book(args, [args.pair])
    _apply_band(args, book)
    surface = book[args.pair]
    expiry = parse_datetime(args.expiry)
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
    expiry = parse_datetime(args.expiry)
    sl = surface.slice_at(expiry, args.method, args.cut)
    print(f"{args.pair}  expiry {expiry:%Y-%m-%d}  t={sl.t:.5f}y  method={args.method}")
    print(f"  {'point':<10}{'K/F':>12}{'vol %':>10}")
    for row in surface.smile_table(expiry, method=args.method, cut=args.cut):
        print(f"  {row['label']:<10}{row['strike']:>12.6f}{row['vol'] * 100:>10.4f}")
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
          f"{'term':>9}{'smile':>9}{'per year':>10}{'/atm':>8}")
    for r in carry:
        if not r.expiry:
            print(f"  {r.tenor:<6}  {r.warnings[0]}")
            continue
        strike = "—" if r.strike != r.strike else f"{r.strike:.5f}"
        print(f"  {r.tenor:<6}{r.forward:>10.5f}{strike:>10}{pct(r.level)}{pct(r.level_rolled)}"
              f"{sgn(r.roll)}{sgn(r.roll_term)}{sgn(r.roll_smile)}{sgn(r.roll_annual, 2, 10)}"
              f"{r.ratio_atm:>+8.3f}")
    for note in dict.fromkeys(w for r in carry for w in r.warnings if r.expiry):
        print(f"  ! {note}")

    fair = analytics.fair_value_table(book, args.pair, hist, horizon_days=args.horizon,
                                      lookback_days=lookback, method=args.method, cut=args.cut,
                                      annualisation=args.annualisation)
    print(f"\nfair value — at-the-money implied against realized plus the at-the-money roll")
    print(f"  {'tenor':<6}{'implied':>9}{'realized':>9}{'window':>8}{'roll':>9}{'x':>7}"
          f"{'roll val':>9}{'of which fwd':>13}{'fair':>9}{'rich':>9}")
    for r in fair:
        print(f"  {r.tenor:<6}{pct(r.implied)}{pct(r.realized)}"
              f"{(r.realized_window_days or 0):>8.0f}{sgn(r.roll)}{r.roll_multiplier:>7.1f}"
              f"{sgn(r.roll_value)}{sgn(r.forward_value, 3, 13)}{pct(r.fair)}{sgn(r.richness)}")

    if hist is not None:
        rows = analytics.realized_table(book, args.pair, hist, lookback_days=lookback,
                                        method=args.method, cut=args.cut,
                                        annualisation=args.annualisation)
        print(f"\nrealized against implied  ({args.annualisation} annualisation)")
        print(f"  {'tenor':<6}{'window':>7}{'obs':>5}{'realized':>9}{'implied':>9}{'premium':>9}"
              f"{'r.skew':>9}{'->tenor':>9}{'i.skew':>9}{'r.kurt':>9}{'->tenor':>9}{'i.kurt':>9}")
        for r in rows:
            if r.observations == 0:
                print(f"  {r.tenor:<6}  {r.warnings[0] if r.warnings else 'unavailable'}")
                continue
            f = lambda v: "—" if v is None or v != v else f"{v:+9.3f}"
            print(f"  {r.tenor:<6}{r.window_days:>7.0f}{r.observations:>5d}{pct(r.realized)}"
                  f"{pct(r.implied)}{sgn(r.premium)}{f(r.realized_skew)}{f(r.realized_skew_scaled)}"
                  f"{f(r.implied_skew)}{f(r.realized_kurtosis)}{f(r.realized_kurtosis_scaled)}"
                  f"{f(r.implied_kurtosis)}")

    if book.data.pairs[args.pair].is_cross:
        tri = analytics.triangle_table(book, args.pair, method=args.method, cut=args.cut,
                                       with_noise=not args.no_noise)
        legs = book.data.pairs[args.pair].legs
        print(f"\ntriangle — {args.pair} against {legs[0]} and {legs[1]} "
              f"(coefficients {tri[0].coefficients if tri else '?'})")
        keys = ["atm", "rr25", "fly25", "rr10", "fly10"]
        print(f"  {'tenor':<6}{'rho':>7}{'implied rho':>12}" +
              "".join(f"{k + ' mk':>11}{k + ' tri':>11}{'diff':>9}" for k in keys))
        for r in tri:
            line = (f"  {r.tenor:<6}{r.rho:>+7.3f}"
                    f"{(r.implied_correlation if r.implied_correlation is not None else float('nan')):>+12.3f}")
            for k in keys:
                line += f"{pct(r.marked.get(k), 3, 11)}{pct(r.triangle.get(k), 3, 11)}{sgn(r.difference.get(k), 3, 9)}"
            print(line)
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

    if args.compare:
        _print_comparison(args, book, history)
    return 0


def _print_comparison(args, book, history) -> None:
    """The curve comparison panel, from the same function the screen calls.

    ``--compare`` is repeatable: ``--compare surface --compare marks
    --compare history:-30d`` is three curves, differenced against the first.
    """
    from . import curves as curves_mod

    panel = curves_mod.ComparePanel(
        curves=tuple(curves_mod.parse_spec(spec, args.pair) for spec in args.compare),
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

    text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text()
    payload = {
        "underlying": args.underlying, "pair": args.pair,
        "invert": args.invert if args.invert is not None else None,
        "scale": args.scale, "expiry": args.expiry, "forward": args.forward,
        "text": text, "vol_unit": args.vol_unit, "beta": args.beta,
        "weighting": args.weighting, "cut": args.cut, "method": args.method,
        "strike_column": args.strike_column, "vol_column": args.vol_column,
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
    print(f"  SABR   alpha={f['alpha']:.6f}  rho={f['rho']:+.4f}  "
          f"nu={f['volvol']:.4f} (nu*sqrt(t)={f['log_volvol']:.4f})  beta={f['beta']:g}")
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
    return 1 if r["warnings"] else 0


def cmd_mm(args) -> int:
    """Fit a curve, fine tune the wings and build a two-way price.

    The web screen's Market-maker tab and this command share
    ``marketmaker.panel_from_request``, so a panel set up in the browser and
    the same panel run from a shell script produce identical numbers.
    """
    from . import marketmaker as mm
    from .knowledge import KnowledgeBank

    text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text()
    payload = {
        "pair": args.pair, "cut": args.cut, "method": args.method, "text": text,
        "vol_unit": args.vol_unit, "fly_convention": args.fly,
        "target_source": args.target_source,
        "target_text": (Path(args.target).read_text() if args.target else ""),
        "fit_curve": not args.no_curve, "free": args.free,
        "tune_wings": not args.no_wings, "smile_free": args.smile_free,
        "mid_pull": args.mid_pull, "max_nfev": args.max_evals,
        "vega_text": (Path(args.vega).read_text() if args.vega else ""),
        "vega_scale": args.axe_scale, "fair_weight": args.fair_weight,
        "axe_weight": args.axe_weight, "skew_cap": args.skew_cap,
        "horizon_days": args.horizon, "fallback_spread": args.fallback_spread,
        "apply": False,
    }
    bank = KnowledgeBank.load(args.knowledge)
    clock = _clock(args)

    if args.learn:
        book = Book.from_excel(args.workbook, clock)
        rules, notes, parse = mm.learn_from_panel(payload, clock)
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

    book = _book(args, [args.pair])
    hist = None
    if args.history:
        from .history import load_history
        loaded = load_history(args.history)
        if args.pair in loaded:
            hist = loaded[args.pair]
        else:
            print(f"  ! {args.history} has no sheet for {args.pair}", file=sys.stderr)
    r = mm.panel_from_request(payload).run(book, bank=bank, hist=hist)

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

    sheet = r["sheet"]
    print(f"\n  {sheet['n_quotes']} quote(s), {sheet['inside']} with our mid inside their market, "
          f"{sheet['priced']} with a width")
    panel_cap = float(args.skew_cap)
    print(f"  {'quote':<26}{'their bid':>10}{'their ask':>10}{'model':>9}{'skew':>9}"
          f"{'our bid':>9}{'our ask':>10}{'width':>8}  verdict")

    def cell(value, width=10, dp=4, signed=False):
        if value is None:
            return f"{'-':>{width}}"
        return f"{value:>+{width}.{dp}f}" if signed else f"{value:>{width}.{dp}f}"

    capped = 0
    for row in sheet["rows"]:
        # A starred lean is one the cap bound: the axe wanted to move the price
        # further than a quote is allowed to be moved.
        if row["skew_capped"]:
            capped += 1
        print(f"  {row['describe']:<26}{cell(row['market_bid'])}{cell(row['market_ask'])}"
              f"{cell(row['model_after'], 9)}{cell(row['skew_total'], 8, 3, True)}"
              f"{'*' if row['skew_capped'] else ' '}"
              f"{cell(row['our_bid'], 9)}{cell(row['our_ask'])}{cell(row['width'], 8, 3)}"
              f"  {row['verdict']}")
        if row["width_source"]:
            print(f"      . width: {row['width_source']}")
        if row["skew_reason"]:
            print(f"      . {row['skew_reason']}")
        for x in row["advice"]:
            print(f"      > {x}")
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
    return 1 if r["warnings"] else 0


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


def cmd_serve(args) -> int:
    from .webapp import serve
    serve(args.workbook, host=args.host, port=args.port,
          clock=_clock(args), open_browser=not args.no_browser, feed_path=args.feed,
          history_path=getattr(args, "history", None),
          bank_path=getattr(args, "knowledge", None))
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

    s = add_command("serve", parents=[common], help="run the local web interface")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV (pair,tenor,value)")
    s.add_argument("--history", default=_default_history(),
                   help="historical workbook for the analysis screen")
    s.add_argument("--knowledge", help="knowledge bank JSON (default: beside the workbook)")
    s.set_defaults(func=cmd_serve)

    s = add_command("mm", parents=[common],
                       help="fit a curve to a market, fine tune the wings and quote it")
    s.add_argument("pair")
    s.add_argument("--file", default="-", help="pasted market quotes (default: stdin)")
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
    s.add_argument("--knowledge", help="knowledge bank JSON (default: beside the workbook)")
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
    s.add_argument("expiry", help="e.g. 2024-05-28")
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
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.add_argument("--no-noise", action="store_true",
                   help="skip the triangle's reconstruction diagnostic (faster)")
    s.add_argument("--compare", action="append", default=[], metavar="SOURCE",
                   help="add a curve to the comparison table; repeatable. "
                        "'surface', 'marks', 'history', 'history:2024-01-15', "
                        "'history:-30d', 'history:-30d:EURUSD'. The first is the "
                        "one the others are differenced against")
    s.add_argument("--field", default="atm", choices=list(_curve_fields()),
                   help="which quoted number the comparison table shows")
    s.set_defaults(func=cmd_analysis)

    s = add_command("listed", parents=[common],
                       help="fit SABR to an exchange-traded strike/vol table")
    s.add_argument("underlying", help=f"contract code, or CUSTOM with --pair")
    s.add_argument("--expiry", required=True, help="e.g. '2026-09-11 19:00' (the exchange's own cut)")
    s.add_argument("--forward", type=float, required=True, help="futures price in the listed units")
    s.add_argument("--file", default="-", help="strike/vol table (default: stdin)")
    s.add_argument("--pair", help="override the FX pair this contract maps to")
    s.add_argument("--invert", type=lambda v: v.lower() in ("1", "true", "yes", "on"),
                   default=None, help="override whether listed strikes are the reciprocal of the pair")
    s.add_argument("--scale", type=float, help="multiply quoted strikes before mapping, e.g. 1e-6")
    s.add_argument("--beta", type=float, default=1.0, help="SABR beta (default 1, lognormal)")
    s.add_argument("--weighting", default="vega", choices=["vega", "equal", "table"])
    s.add_argument("--vol-unit", default="auto", choices=["auto", "percent", "decimal"])
    s.add_argument("--strike-column", type=int, help="1-based, overrides header detection")
    s.add_argument("--vol-column", type=int, help="1-based, overrides header detection")
    s.add_argument("--cut", default="NY")
    s.add_argument("--method", default="SVI")
    s.add_argument("--no-compare", action="store_true", help="fit only; do not load the book")
    s.set_defaults(func=cmd_listed)

    s = add_command("smile", parents=[common, band_opts],
                       help="print the smile at one expiry")
    s.add_argument("pair")
    s.add_argument("expiry")
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
    takes_value = {"-w", "--workbook", "--asof", "--enable-tab", "--config"}
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


def main(argv=None) -> int:
    from . import preflight

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
