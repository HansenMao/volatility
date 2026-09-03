"""volkit -- FX volatility surface modelling.

A rebuild of the original ``vol`` tool, organised so that the numerics, the
market data and the user interface are separable.

    from volkit import Book, Clock

    book = Book.from_excel("files/vol_marks.xlsx").load_all()
    usdjpy = book["USDJPY"]
    usdjpy.vol(1.02, "2024-05-28")        # strike/forward ratio -> implied vol
    usdjpy.atm_vol("2024-05-28", "TK")    # ATM at the Tokyo cut
    usdjpy.risk_reversal("2024-05-28", 0.25)

The names above are bound **lazily**, on first use, rather than imported when
the package is.  That is not a startup-time optimisation; it is what lets the
package be imported at all before its dependencies exist.

``build_exe.py`` reads ``volkit.screens`` to find out which screens a build
should contain, and it does that *before* its own dependency-install step --
it is the thing that installs numpy, scipy and pandas in the first place.  An
eager ``from .atm import AtmCurve`` here dragged the entire numeric stack in
behind ``from volkit import screens``, so on a machine that did not have it
yet the packaging script died at ``import numpy`` before printing its first
line.  Nothing in ``screens``, ``paths`` or ``config`` needs numpy, and now
nothing makes them ask for it.
"""

from importlib import import_module

__version__ = "2.0.0"

#: Public name -> the submodule that defines it.  One entry per name in
#: ``__all__``; a name here that no longer exists is caught by a test rather
#: than by an AttributeError in somebody's script.
_EXPORTS = {
    "AtmCurve": "atm",
    "BackboneParams": "atm",
    "DeltaConvention": "black",
    "Book": "book",
    "CalendarSet": "calendars",
    "CorrelationCurve": "cross",
    "CrossAtmCurve": "cross",
    "Event": "events",
    "EventSchedule": "events",
    "ExcelSource": "marketdata",
    "MarketData": "marketdata",
    "MarketDataError": "marketdata",
    "ConvergenceError": "numerics",
    "SmileSlice": "smile",
    "SVIParams": "smile",
    "SabrParams": "sabr",
    "SmileMark": "surface",
    "VolSurface": "surface",
    "Clock": "timeutil",
    "tenor_to_years": "timeutil",
    "TimeWeighting": "timeweight",
    "VegaWeights": "vegaweights",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    """Import the defining submodule on first use (PEP 562).

    The resolved object is written into the module namespace, so this runs
    once per name and every later lookup is an ordinary attribute access.
    """
    where = _EXPORTS.get(name)
    if where is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{where}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))
