"""Startup environment checks, with messages that say what to do.

The one that matters is the time zone database.  Since cut times, the weekly
market close and the economic calendar are all resolved through ``zoneinfo``,
and Windows ships no IANA database at all, a Windows build without the
``tzdata`` package fails at the first cut calculation with an error that gives
no hint of the cause.  Better to say so at startup.
"""

from __future__ import annotations

import sys

REQUIRED_ZONES = (
    "America/New_York", "America/Toronto", "Europe/London", "Europe/Berlin",
    "Europe/Zurich", "Asia/Tokyo", "Asia/Hong_Kong", "Asia/Shanghai",
    "Australia/Sydney", "Pacific/Auckland",
)


def check_timezones() -> list[str]:
    """Confirm every zone the model needs can actually be loaded."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    missing = []
    for zone in REQUIRED_ZONES:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, KeyError, ValueError):
            missing.append(zone)
    if not missing:
        return []
    return [
        f"the time zone database is unavailable ({len(missing)} of "
        f"{len(REQUIRED_ZONES)} zones missing, e.g. {missing[0]}). "
        f"Windows ships no IANA database, so install it with:  pip install tzdata"
        f"\n  Without it, cut times, the weekly market close and the economic "
        f"calendar cannot be resolved."
    ]


def check_packages() -> list[str]:
    out = []
    for name, why in (("numpy", "numerics"), ("scipy", "solvers and special functions"),
                      ("pandas", "workbook reading"), ("openpyxl", "xlsx support")):
        try:
            __import__(name)
        except ImportError:
            out.append(f"{name} is not installed but is required for {why}")
    return out


def run(verbose: bool = True) -> list[str]:
    """All checks.  Returns problems; empty means good to go."""
    problems = check_packages() + check_timezones()
    if verbose and problems:
        print("volkit startup checks found problems:", file=sys.stderr)
        for p in problems:
            print(f"  ! {p}", file=sys.stderr)
    return problems
