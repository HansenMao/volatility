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

from .book import Book
from .marketdata import ExcelSource, MarketDataError
from .timeutil import UTC, Clock, parse_datetime, tenor_to_years

def _default_workbook() -> str:
    """The workbook beside the executable, or the project copy when running from source."""
    from .paths import find_data_file
    found = find_data_file("vol_marks.xlsx", "files/vol_marks.xlsx")
    return str(found) if found else "files/vol_marks.xlsx"


def _default_feed() -> str | None:
    from .paths import find_data_file
    found = find_data_file("market_feed.csv", "files/market_feed.csv")
    return str(found) if found else None


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
          clock=_clock(args), open_browser=not args.no_browser, feed_path=args.feed)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="volkit", description="FX volatility surface toolkit")
    p.add_argument("-w", "--workbook", default=_default_workbook(), help="market data workbook")
    p.add_argument("--asof", help="valuation datetime, e.g. '2024-02-28 12:00' (default: now, UTC)")

    # The same two options are attached to every subcommand as well, so they
    # work in either position.  People naturally type
    # "volkit tenors USDJPY --asof ..." rather than putting the option before
    # the subcommand, and argparse rejects that unless the subparser knows it
    # too.  SUPPRESS stops the subparser overwriting a value given earlier
    # with its own default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-w", "--workbook", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    common.add_argument("--asof", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", parents=[common], help="validate the workbook without building anything")
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("serve", parents=[common], help="run the local web interface")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--feed", default=_default_feed(),
                   help="spot / forward feed CSV (pair,tenor,value)")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("validate", parents=[common], help="hunt for competing smile calibrations")
    s.add_argument("pair", nargs="?", help="default: every pair")
    s.set_defaults(func=cmd_validate)

    s = sub.add_parser("events", parents=[common], help="scheduled economic events for a pair")
    s.add_argument("pair")
    s.add_argument("--horizon", type=float, default=1.0, help="years")
    s.set_defaults(func=cmd_events)

    s = sub.add_parser("tenors", parents=[common], help="print the ATM term structure")
    s.add_argument("pair")
    s.add_argument("--cut", default="TK")
    s.set_defaults(func=cmd_tenors)

    s = sub.add_parser("daily", parents=[common], help="export the daily volatility series")
    s.add_argument("pair")
    s.add_argument("--horizon", type=float, default=1.0, help="years")
    s.add_argument("--cut", default="NY")
    s.add_argument("--field", default="cumulative", choices=["cumulative", "daily"])
    s.add_argument("--out", help="output file (default: stdout)")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("vol", parents=[common], help="volatility for a strike and expiry")
    s.add_argument("pair")
    s.add_argument("expiry", help="e.g. 2024-05-28")
    s.add_argument("--strike", type=float)
    s.add_argument("--forward", type=float, default=1.0)
    s.add_argument("--method", default="SVI")
    s.add_argument("--cut", default="TK")
    s.set_defaults(func=cmd_vol)

    s = sub.add_parser("smile", parents=[common], help="print the smile at one expiry")
    s.add_argument("pair")
    s.add_argument("expiry")
    s.add_argument("--method", default="SVI")
    s.add_argument("--cut", default="TK")
    s.set_defaults(func=cmd_smile)
    return p


def main(argv=None) -> int:
    from . import preflight
    problems = preflight.run()
    if any("not installed" in p or "time zone database" in p for p in problems):
        return 3
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (MarketDataError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
