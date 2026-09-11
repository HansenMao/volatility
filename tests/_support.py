"""Shared imports, paths and helpers for the volkit test modules.

This was the preamble of ``tests/test_volkit.py``, which had grown to 15,008
lines in one file.  The classes now live in ``test_numerics.py``,
``test_workbook.py``, ``test_marketmaker.py`` and the rest, so a change to one
area can be re-run in seconds instead of re-running the whole suite; everything
they share is here, and each of them starts with ``from ._support import *``.

Nothing here is a test: the discovery pattern is ``test*.py``, so this module is
imported by the test modules and never collected as one.
"""

from __future__ import annotations

import atexit
import math
import shutil
import tempfile
import textwrap
import unittest
import inspect as _inspect

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from volkit import black, sabr, smile
from volkit.atm import AtmCurve, BackboneParams
from volkit.black import DeltaConvention
from volkit.book import Book
from volkit.calendars import DEFAULT_CALENDARS, CalendarSet, easter
from volkit.cross import (CorrelationCurve, CrossAtmCurve, dollar_legs,
                          infer_leg_signs)
from volkit import exotics
from volkit.banded import Band, BetaBandSmile, JumpSpec, calibrate_band_smile, load_bands
from volkit.feed import FeedError, MarketFeed, pip_divisor
from volkit import analytics, discount, history, listed, marketmaker, moments, quotes
from volkit.events import EventSchedule
from volkit.knowledge import KnowledgeBank, PairKnowledge, Rule
from volkit import marketdata
from volkit.marketdata import ExcelSource, MarketData, MarketDataError
from volkit.numerics import ConvergenceError, fixed_point, integrate_piecewise, solve_scalar
from volkit.pricing import (OptionLeg, StrikeSpec, expiry_datetime, parse_strike, price_strip,
                            quick_vol, resolve_expiry)
from volkit.smile import SmileSlice, fit_svi
from volkit.surface import (PARAM_NAMES, QUOTE_FIELDS, SmileMark, VolSurface,
                            fit_param_term_structure)
from volkit.timeutil import (Clock, DAYS_IN_YEAR, TenorError, UTC, add_tenor,
                             normalise_tenor, parse_datetime, parse_tenor,
                             tenor_key, tenor_to_years)
from volkit.timeweight import DEFAULT_SESSION_HOURS, TimeWeighting, session_shares

from ._fixtures import PAIR_SHEETS, make_workbook


def _source(*parts: str) -> str:
    """A file of this project, read as UTF-8.

    Never in the locale's encoding: that is cp1252 on the Windows box the
    build runs on, ``index.html`` is full of em dashes, and the whole suite
    ended there with "'charmap' codec can't decode byte 0x81".
    """
    return Path(__file__).resolve().parents[1].joinpath(*parts).read_text(encoding="utf-8")


#: The workbook that ships beside the exe.  **Do not pin numbers off this.**
#: It is a spreadsheet, and a spreadsheet is somebody's file: an export of the
#: desk's own book landed on it once and a hundred tests failed at once, over
#: three Windows builds, for reasons that had nothing to do with the code.  Use
#: it only where the shipped file itself is what is under test -- that it is
#: readable, that its tabs are the ones ``configsheets`` expects, that its
#: 10-delta columns are still array formulas a save must not flatten.  Say so in
#: the test when you do.  Everything else uses ``BOOK`` below.
WORKBOOK = Path(__file__).resolve().parents[1] / "files" / "vol_marks.xlsx"

ASOF = Clock(datetime(2024, 2, 28, 12, 0, tzinfo=UTC))
FEED = Path(__file__).resolve().parents[1] / "files" / "market_feed.csv"
HISTORY = Path(__file__).resolve().parents[1] / "files" / "history_sample.xlsx"

#: A workbook built from ``tests/_fixtures.py``, which is the shipped file's
#: values written out as literals the suite owns.  Every number is the one
#: ``WORKBOOK`` holds today, so a test reads exactly what it read before -- and
#: nothing anyone does to the spreadsheet can change it again.
#:
#: Written once per process into a temporary directory and left read-only by
#: convention: a test that *modifies* a workbook wants its own copy, from
#: ``book_copy()`` below.
_FIXTURE_DIR = Path(tempfile.mkdtemp(prefix="volkit-fixture-"))
atexit.register(shutil.rmtree, _FIXTURE_DIR, True)
BOOK = make_workbook(_FIXTURE_DIR / "vol_marks.xlsx")


def book_copy(directory, pairs=None, name="vol_marks.xlsx"):
    """A writable fixture workbook in ``directory``.

    The replacement for ``shutil.copy(WORKBOOK, wb)``.  With no ``pairs`` it is
    the same workbook, cell for cell, so nothing a test pins has to move.

    ``pairs`` writes the settings tabs whole and only those pair sheets, which
    is worth doing whenever a test names the pairs it touches: every load
    calibrates every pair CONFIG lists, so a test that marks USDJPY and reads it
    back was fitting thirteen other smiles each time it read the file.
    """
    return make_workbook(Path(directory) / name, pairs=pairs)


_SMALL_BOOKS = {}


def book_for(*pairs):
    """A read-only fixture workbook carrying only ``pairs``, built once.

    ``BOOK`` for a test that names the pairs it reads.  Use it wherever a
    service is pointed at a workbook and never writes to it; ``book_copy`` is
    the one to use when the test saves.
    """
    key = tuple(sorted(p.upper() for p in pairs))
    if key not in _SMALL_BOOKS:
        _SMALL_BOOKS[key] = make_workbook(
            _FIXTURE_DIR / ("-".join(key).lower() + ".xlsx"), pairs=key)
    return _SMALL_BOOKS[key]


def _flat_distribution(vol, t, n=1601, span=6.0):
    """A lognormal with no smile, built the same way a real one is."""
    sd = vol * math.sqrt(t)
    x = np.linspace(-span * sd, span * sd, n)
    k = np.exp(x)
    c = np.asarray(black.price(1.0, k, vol, t, True), dtype=float)
    cdf = 1.0 + np.gradient(c, x, edge_order=2) / k
    return moments.Distribution(x=x, pdf=np.gradient(cdf, x, edge_order=2), cdf=cdf, t=t)


def hist_point(curve, tenor):
    return next((p for p in curve["points"] if p["tenor"].upper() == tenor.upper()), None)


from volkit import kace


#: Everything the split test modules pull in with ``import *``.  Spelled out
#: rather than left implicit so an accidental new global does not leak into
#: twelve modules at once.
__all__ = [
    'ASOF',
    'AtmCurve',
    'BOOK',
    'BackboneParams',
    'Band',
    'BetaBandSmile',
    'Book',
    'CalendarSet',
    'Clock',
    'ConvergenceError',
    'CorrelationCurve',
    'CrossAtmCurve',
    'DAYS_IN_YEAR',
    'DEFAULT_CALENDARS',
    'DEFAULT_SESSION_HOURS',
    'DeltaConvention',
    'EventSchedule',
    'ExcelSource',
    'FEED',
    'FeedError',
    'HISTORY',
    'JumpSpec',
    'KnowledgeBank',
    'MarketData',
    'MarketDataError',
    'MarketFeed',
    'OptionLeg',
    'PAIR_SHEETS',
    'PARAM_NAMES',
    'PairKnowledge',
    'Path',
    'QUOTE_FIELDS',
    'Rule',
    'SmileMark',
    'SmileSlice',
    'StrikeSpec',
    'TenorError',
    'TimeWeighting',
    'UTC',
    'VolSurface',
    'WORKBOOK',
    'ZoneInfo',
    '_FIXTURE_DIR',
    '_SMALL_BOOKS',
    '_flat_distribution',
    '_inspect',
    '_source',
    'add_tenor',
    'analytics',
    'annotations',
    'atexit',
    'black',
    'book_copy',
    'book_for',
    'calibrate_band_smile',
    'date',
    'datetime',
    'discount',
    'dollar_legs',
    'easter',
    'exotics',
    'expiry_datetime',
    'fit_param_term_structure',
    'fit_svi',
    'fixed_point',
    'hist_point',
    'history',
    'infer_leg_signs',
    'integrate_piecewise',
    'kace',
    'listed',
    'load_bands',
    'make_workbook',
    'marketdata',
    'marketmaker',
    'math',
    'moments',
    'normalise_tenor',
    'np',
    'parse_datetime',
    'parse_strike',
    'parse_tenor',
    'pd',
    'pip_divisor',
    'price_strip',
    'quick_vol',
    'quotes',
    'resolve_expiry',
    'sabr',
    'session_shares',
    'shutil',
    'smile',
    'solve_scalar',
    'sys',
    'tempfile',
    'tenor_key',
    'tenor_to_years',
    'textwrap',
    'timedelta',
    'unittest',
]
