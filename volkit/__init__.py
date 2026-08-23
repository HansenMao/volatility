"""volkit -- FX volatility surface modelling.

A rebuild of the original ``vol`` tool, organised so that the numerics, the
market data and the user interface are separable.

    from volkit import Book, Clock

    book = Book.from_excel("files/vol_marks.xlsx").load_all()
    usdjpy = book["USDJPY"]
    usdjpy.vol(1.02, "2024-05-28")        # strike/forward ratio -> implied vol
    usdjpy.atm_vol("2024-05-28", "TK")    # ATM at the Tokyo cut
    usdjpy.risk_reversal("2024-05-28", 0.25)
"""

from .atm import AtmCurve, BackboneParams
from .black import DeltaConvention
from .book import Book
from .calendars import CalendarSet
from .cross import CorrelationCurve, CrossAtmCurve
from .events import Event, EventSchedule
from .marketdata import ExcelSource, MarketData, MarketDataError
from .numerics import ConvergenceError
from .smile import SmileSlice, SVIParams
from .sabr import SabrParams
from .surface import SmileMark, VolSurface
from .timeutil import Clock, tenor_to_years
from .timeweight import TimeWeighting

__version__ = "2.0.0"

__all__ = [
    "AtmCurve", "BackboneParams", "Book", "CalendarSet", "Clock", "ConvergenceError",
    "CorrelationCurve", "CrossAtmCurve", "DeltaConvention", "Event", "EventSchedule",
    "ExcelSource", "MarketData", "MarketDataError", "SabrParams", "SmileMark",
    "SmileSlice", "SVIParams", "TimeWeighting", "VolSurface", "tenor_to_years",
]
