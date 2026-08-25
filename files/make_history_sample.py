"""Regenerate ``files/history_sample.xlsx``.

The analysis screen reads a second workbook holding what the market did, not
what it is marked at now: one sheet per pair, one row per past date, columns
for spot, forwards and the quoted volatility surface.  Nobody keeps that file
in a fixed layout, so ``volkit.history`` matches columns by reading their
headers -- and this sample is deliberately inconsistent between sheets to show
what that tolerates.  EURUSD spells its columns one way, USDJPY another, and
GBPUSD carries a column that cannot be understood at all so the reader has
something to complain about.

The numbers are synthetic and seeded, so the file regenerates byte-stable and
the tests can pin exact values.  EURJPY is the exact product of the two dollar
pairs, which is what makes the cross triangle on the analysis screen mean
something on this data.

    python3 files/make_history_sample.py
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parent / "history_sample.xlsx"
START, END = date(2022, 1, 3), date(2024, 2, 28)
TENORS = ("1W", "1M", "3M", "6M", "1Y")
TENOR_YEARS = {"1W": 7 / 365.2425, "1M": 1 / 12, "3M": 0.25, "6M": 0.5, "1Y": 1.0}

# Realized vol each pair is simulated at, the risk premium its ATM is quoted
# over that, and the shape of its smile.  Chosen to look like a real desk's
# marks rather than to be round numbers.
# The levels are chosen so the sample lines up with ``files/vol_marks.xlsx``:
# each pair's quoted 3M ATM lands within a tenth of a vol point of what that
# workbook marks, and the smile shapes match its risk reversals and flies.  A
# sample that disagreed with the shipped marks would make the analysis screen
# look broken the first time anyone opened it.
SPEC = {
    "EURUSD": dict(spot0=1.1350, vol=0.0524, premium=0.0075, rr25=-0.0008, bf25=0.0022,
                   carry=-0.0180),
    "USDJPY": dict(spot0=115.20, vol=0.0555, premium=0.0060, rr25=-0.0062, bf25=0.0029,
                   carry=+0.0420),
    "GBPUSD": dict(spot0=1.3520, vol=0.0691, premium=0.0068, rr25=-0.0080, bf25=0.0030,
                   carry=-0.0090),
}
RHO_EURUSD_USDJPY = +0.40          # the correlation vol_marks.xlsx builds EURJPY at


def business_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def simulate(dates, spot0, vol, shocks):
    """Spot from a driftless lognormal walk on the supplied standard shocks."""
    dt = 1.0 / 252.0
    steps = vol * math.sqrt(dt) * shocks - 0.5 * vol * vol * dt
    return spot0 * np.exp(np.concatenate([[0.0], np.cumsum(steps)]))[: len(dates)]


def term(base: float, t: float) -> float:
    """A gently upward-sloping term structure around ``base``."""
    return base * (0.93 + 0.10 * math.sqrt(t / 1.0))


def surface_columns(n, spot, rng, spec, vol_scale=None):
    """Quoted ATM, risk reversals and butterflies, with a little daily noise."""
    cols = {}
    for tenor in TENORS:
        t = TENOR_YEARS[tenor]
        level = term(spec["vol"] + spec["premium"], t)
        if vol_scale is not None:
            level = term(vol_scale, t)
        wobble = 1.0 + 0.045 * rng.standard_normal(n).cumsum() / math.sqrt(max(n, 1))
        cols[("atm", tenor)] = np.round(level * wobble * 100.0, 4)
        for d, scale in ((25, 1.0), (10, 1.85)):
            rr = spec["rr25"] * scale * (0.85 + 0.35 * math.sqrt(t))
            bf = spec["bf25"] * (scale ** 1.80) * (0.9 + 0.25 * math.sqrt(t))
            cols[("rr", d, tenor)] = np.round(
                (rr + 0.0001 * rng.standard_normal(n)) * 100.0, 4)
            cols[("bf", d, tenor)] = np.round(
                (bf + 0.0001 * rng.standard_normal(n)) * 100.0, 4)
    return cols


def main() -> None:
    dates = business_days(START, END)
    n = len(dates)
    rng = np.random.default_rng(20240228)

    z1 = rng.standard_normal(n - 1)
    z2 = RHO_EURUSD_USDJPY * z1 + math.sqrt(1 - RHO_EURUSD_USDJPY ** 2) * rng.standard_normal(n - 1)
    z3 = 0.72 * z1 + math.sqrt(1 - 0.72 ** 2) * rng.standard_normal(n - 1)

    spots = {
        "EURUSD": simulate(dates, SPEC["EURUSD"]["spot0"], SPEC["EURUSD"]["vol"], z1),
        "USDJPY": simulate(dates, SPEC["USDJPY"]["spot0"], SPEC["USDJPY"]["vol"], z2),
        "GBPUSD": simulate(dates, SPEC["GBPUSD"]["spot0"], SPEC["GBPUSD"]["vol"], z3),
    }
    spots["EURJPY"] = spots["EURUSD"] * spots["USDJPY"]

    sheets: dict[str, pd.DataFrame] = {}

    # -- EURUSD: outright forwards, "ATM 1M" style headers -----------------
    spec = SPEC["EURUSD"]
    px = spots["EURUSD"]
    frame = {"Date": dates, "Spot": np.round(px, 5)}
    for tenor in TENORS:
        t = TENOR_YEARS[tenor]
        frame[f"Fwd {tenor}"] = np.round(px * math.exp(spec["carry"] * t), 5)
    cols = surface_columns(n, px, rng, spec)
    for tenor in TENORS:
        frame[f"ATM {tenor}"] = cols[("atm", tenor)]
    for d in (25, 10):
        for tenor in TENORS:
            frame[f"RR{d} {tenor}"] = cols[("rr", d, tenor)]
            frame[f"BF{d} {tenor}"] = cols[("bf", d, tenor)]
    sheets["EURUSD"] = pd.DataFrame(frame)

    # -- USDJPY: forward *points*, lower case, "1M ATM vol" style ----------
    spec = SPEC["USDJPY"]
    px = spots["USDJPY"]
    frame = {"date": dates, "spot": np.round(px, 4)}
    for tenor in TENORS:
        t = TENOR_YEARS[tenor]
        pts = (px * math.exp(spec["carry"] * t) - px) * 100.0     # JPY pips
        frame[f"{tenor} swap points"] = np.round(pts, 3)
    cols = surface_columns(n, px, rng, spec)
    for tenor in TENORS:
        frame[f"{tenor} atm vol"] = cols[("atm", tenor)]
    for d in (25, 10):
        for tenor in TENORS:
            frame[f"{tenor} {d}d rr"] = cols[("rr", d, tenor)]
            frame[f"{tenor} {d}d fly"] = cols[("bf", d, tenor)]
    sheets["USDJPY"] = pd.DataFrame(frame)

    # -- EURJPY: the exact product of the two, with a triangled surface ----
    px = spots["EURJPY"]
    frame = {"Date": dates, "Spot": np.round(px, 4)}
    for tenor in TENORS:
        t = TENOR_YEARS[tenor]
        carry = SPEC["EURUSD"]["carry"] + SPEC["USDJPY"]["carry"]
        frame[f"Forward {tenor}"] = np.round(px * math.exp(carry * t), 4)
    va, vb = SPEC["EURUSD"]["vol"] + SPEC["EURUSD"]["premium"], SPEC["USDJPY"]["vol"] + SPEC["USDJPY"]["premium"]
    cross_vol = math.sqrt(va * va + vb * vb + 2 * RHO_EURUSD_USDJPY * va * vb)
    cross_spec = dict(SPEC["EURUSD"], rr25=-0.0076, bf25=0.0030)
    cols = surface_columns(n, px, rng, cross_spec, vol_scale=cross_vol)
    for tenor in TENORS:
        frame[f"ATM {tenor}"] = cols[("atm", tenor)]
    for d in (25, 10):
        for tenor in TENORS:
            frame[f"RR {d}d {tenor}"] = cols[("rr", d, tenor)]
            frame[f"BF {d}d {tenor}"] = cols[("bf", d, tenor)]
    sheets["EURJPY"] = pd.DataFrame(frame)

    # -- GBPUSD: a short surface, plus a column nothing can read -----------
    spec = SPEC["GBPUSD"]
    px = spots["GBPUSD"]
    frame = {"Date": dates, "Spot": np.round(px, 5)}
    cols = surface_columns(n, px, rng, spec)
    for tenor in ("1M", "3M", "1Y"):
        frame[f"Fwd {tenor}"] = np.round(px * math.exp(spec["carry"] * TENOR_YEARS[tenor]), 5)
        frame[f"ATM {tenor}"] = cols[("atm", tenor)]
        frame[f"RR25 {tenor}"] = cols[("rr", 25, tenor)]
        frame[f"BF25 {tenor}"] = cols[("bf", 25, tenor)]
    frame["Trader note"] = ["" for _ in dates]
    sheets["GBPUSD Curncy"] = pd.DataFrame(frame)

    with pd.ExcelWriter(OUT, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    print(f"wrote {OUT} — {len(sheets)} sheets, {n} rows, {dates[0]} to {dates[-1]}")


if __name__ == "__main__":
    main()
