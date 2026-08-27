"""Market data loading, with the workbook layout treated as one possible source.

The legacy ``Vols`` read the spreadsheet by magic positional index --
``res_line[0]`` was the initial vol, ``res_line[5]`` the rate correlation --
and anything from row 7 onwards was assumed to be an event.  Inserting a row
silently repriced the book.  It also overloaded the same three cells to mean
initial/long-term/mean-reversion for a single pair but initial/final/decay
*correlation* for a cross, with no indication in the sheet which was which.

Here the sheet is parsed into explicit, validated dataclasses.  Rows are found
by name, units are converted in one place, and every problem is collected and
reported rather than raised on the first bad cell, so a trader sees all the
issues in the workbook at once.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .cross import dollar_legs, infer_leg_signs, is_cross as pair_is_cross
from .surface import SmileMark
from .timeutil import UTC, parse_datetime

# Row labels in the PARAMS sheet, matched case- and space-insensitively.
PARAM_ROWS = {
    "initial": "initial",
    "longterm": "long_term",
    "long term": "long_term",
    "ratevol": "rate_vol",
    "rate vol": "rate_vol",
    "addon": "short_addon",
    "mr": "mean_reversion",
    "ratecorr": "rate_corr",
    "rate corr": "rate_corr",
    "shortdecay": "short_decay",
    "short decay": "short_decay",
}

# The one column a CONFIG sheet must have: the pairs to build.  "BASE" is
# the legacy spelling of the same column and is read the same way -- it used
# to mean "dollar pairs only", and a sheet still written that way lists its
# crosses in COR beside it.
CONFIG_PAIR_COLUMNS = ("pairs", "pair", "currency pairs", "currency pair",
                       "target pairs", "target", "base")

SMILE_COLUMNS = {
    "expiry": "tenor",
    "st 10d": "st_10",
    "st 25d": "st_25",
    "rr 25d": "rr_25",
    "rr 10d": "rr_10",
}

VOL_POINT = 100.0  # the workbook quotes vol in points; the model works in decimals


class MarketDataError(ValueError):
    """Raised when the source data cannot be interpreted."""


def _norm(text) -> str:
    return str(text).strip().lower().replace("_", " ")


@dataclass
class PairSpec:
    """How one traded pair is built."""

    name: str
    is_cross: bool = False
    legs: tuple[str, ...] = ()
    premium_adjusted: bool | None = None
    #: The legs were worked out from the name rather than named in the sheet.
    derived: bool = False
    #: This pair is here because that cross needs it, not because it was
    #: listed.  Empty for anything the sheet actually asked for.
    implied_by: str = ""

    def resolved_premium_adjusted(self) -> bool:
        if self.premium_adjusted is not None:
            return self.premium_adjusted
        return self.name[:3].upper() == "USD"


@dataclass
class PairParams:
    """Backbone (or correlation) parameters plus events, in decimals."""

    name: str
    initial: float = 0.0
    long_term: float = 0.0
    mean_reversion: float = 5.0
    short_addon: float = 0.0
    short_decay: float = 50.0
    rate_vol: float = 0.0
    rate_corr: float = 0.0
    events: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class MarketData:
    """Everything needed to build a book of surfaces."""

    pairs: dict[str, PairSpec] = field(default_factory=dict)
    params: dict[str, PairParams] = field(default_factory=dict)
    marks: dict[str, list[SmileMark]] = field(default_factory=dict)
    tenor_points: tuple[str, ...] = ("1w", "2w", "3w", "1m", "2m", "3m", "6m", "9m", "1y")
    problems: list[str] = field(default_factory=list)
    #: Things the reader worked out rather than read: a cross broken into its
    #: dollar legs, a leg added because a cross needed it.  Not problems --
    #: the workbook is fine -- but never silent either.
    notes: list[str] = field(default_factory=list)
    source: str = ""

    def require_clean(self) -> None:
        if self.problems:
            raise MarketDataError(
                f"{len(self.problems)} problem(s) in {self.source}:\n  - "
                + "\n  - ".join(self.problems)
            )


def open_workbook(path: str | Path) -> "pd.ExcelFile":
    """A reader over an .xlsx, with the file itself already closed.

    ``pd.ExcelFile(path)`` keeps the file open for as long as the reader is
    alive, and openpyxl's workbook is full of parent/child reference cycles,
    so the handle survives until a garbage collection nobody schedules.  On
    Windows that is enough to stop Excel saving the very workbook the tool
    just read: the user is told the file is in use by another program, and the
    other program is this one.

    Reading the bytes first closes the file before any parsing starts, so a
    workbook is open for exactly as long as it takes to copy it and never
    between calls.  Every reader in the project goes through here -- the
    marks, the historical workbook and the forward curve are three different
    files with the same lock.
    """
    path = Path(path)
    with path.open("rb") as fh:
        blob = fh.read()
    return pd.ExcelFile(io.BytesIO(blob))


class ExcelSource:
    """Reader for the ``vol_marks.xlsx`` layout.

    ``event_tz_offset_hours`` reproduces the legacy assumption that event
    timestamps in the sheet are Hong Kong time and must be shifted to UTC.  It
    is a named parameter now rather than a bare ``HKGMTDIFF = 8`` constant
    subtracted in the middle of the loader.
    """

    def __init__(self, path: str | Path, *, event_tz_offset_hours: float = 8.0):
        self.path = Path(path)
        if not self.path.exists():
            raise MarketDataError(f"workbook not found: {self.path}")
        self.event_tz_offset_hours = event_tz_offset_hours

    def load(self) -> MarketData:
        data = MarketData(source=str(self.path))
        try:
            xls = open_workbook(self.path)
        except Exception as exc:  # noqa: BLE001
            raise MarketDataError(f"cannot open {self.path}: {exc}") from exc

        # Closed on the way out however this returns: the reader is over a
        # copy in memory, but leaving readers alive is how the file handle
        # crept back last time.
        with xls:
            sheets = set(xls.sheet_names)
            for required in ("CONFIG", "PARAMS"):
                if required not in sheets:
                    raise MarketDataError(
                        f"{self.path.name} has no {required!r} sheet; found {sorted(sheets)}"
                    )

            self._load_config(xls, data)
            self._load_params(xls, data)
            self._load_marks(xls, data, sheets)
        return data

    # -- CONFIG -----------------------------------------------------------
    def _load_config(self, xls, data: MarketData) -> None:
        """Read the two columns a desk actually maintains: pairs and tenors.

        A pair with the dollar on one side is marked on its own backbone.  A
        pair without one is a **cross**, and a cross is not marked directly:
        it is broken into the two dollar pairs the market quotes -- AUDJPY
        into AUDUSD and USDJPY, EURGBP into EURUSD and GBPUSD -- and what
        gets marked for it is the **correlation** between them.  That is the
        model this tool has always used (``CrossAtmCurve``, and the
        initial / long term / MR cells of a cross's PARAMS column being
        correlation initial / final / decay); until now the sheet had to
        spell it out, with a COR column naming every cross and a further
        column per cross naming its two legs.  Nothing about that was a
        decision -- EURGBP has exactly one sensible pair of legs -- so it is
        worked out from the name instead, by ``cross.dollar_legs``.

        The old layout still loads.  A COR column is read as more pairs, and
        a column named after a cross still names that cross's legs and wins
        over the derived ones, because a sheet that says something explicitly
        must not be second-guessed by a convention.

        A leg nothing listed is **added**: a cross cannot be built without
        both of its legs, and refusing to build the pair somebody asked for
        because they did not also ask for its plumbing is the kind of silent
        gap this project exists to remove.  Every derivation is reported in
        ``data.notes``, because a pair that came out of a convention and one
        that was written down must not read the same.
        """
        cfg = pd.read_excel(xls, "CONFIG")
        cols = {_norm(c): c for c in cfg.columns}
        pair_col = next((cols[k] for k in CONFIG_PAIR_COLUMNS if k in cols), None)
        if pair_col is None:
            raise MarketDataError(
                "CONFIG sheet needs a 'PAIRS' column listing the pairs to build "
                f"(a legacy 'BASE' column is read the same way); found {list(cfg.columns)}"
            )

        listed: list[str] = []
        for column in (pair_col, cols.get("cor")):
            if column is None:
                continue
            for raw in cfg[column].dropna():
                name = str(raw).strip().upper()
                if not name:
                    continue
                if len(name) != 6 or not name.isalpha():
                    data.problems.append(
                        f"CONFIG lists {str(raw).strip()!r}, which is not a six-letter currency pair"
                    )
                    continue
                if name not in listed:
                    listed.append(name)

        derived: list[str] = []
        for name in listed:
            spec = self._pair_spec(name, cfg, cols, data)
            if spec is None:
                continue
            data.pairs[name] = spec
            if spec.derived:
                derived.append(name)

        # A leg nobody listed still has to exist.  The work list is a loop
        # rather than one pass because a leg named in the sheet may itself be
        # a cross; the dollar legs a name is broken into never are.
        implied: list[str] = []
        pending = [n for n, s in data.pairs.items() if s.is_cross]
        while pending:
            owner = pending.pop(0)
            for leg in data.pairs[owner].legs:
                if leg in data.pairs:
                    continue
                spec = self._pair_spec(leg, cfg, cols, data, implied_by=owner)
                if spec is None:
                    continue
                data.pairs[leg] = spec
                implied.append(leg)
                if spec.is_cross:
                    pending.append(leg)

        if derived:
            data.notes.append(
                f"CONFIG: {len(derived)} cross(es) broken into dollar legs and marked by "
                "correlation -- "
                + "; ".join(f"{n} = {' x '.join(data.pairs[n].legs)}" for n in derived)
            )
        if implied:
            data.notes.append(
                "CONFIG: leg(s) added because a cross needs them -- "
                + "; ".join(f"{n} (for {data.pairs[n].implied_by})" for n in implied)
            )

        if "tenors" in cols:
            tenors = tuple(str(x).strip().lower() for x in cfg[cols["tenors"]].dropna())
            if tenors:
                data.tenor_points = tenors

    def _pair_spec(self, name: str, cfg, cols: dict, data: MarketData,
                   implied_by: str = "") -> PairSpec | None:
        """One row of the pairs column, as a spec: dollar pair or cross.

        ``None`` when the sheet named a cross's legs and they cannot build it;
        the problem is recorded rather than raised, so a trader sees every
        bad cell in the workbook at once.
        """
        if not pair_is_cross(name):
            # A dollar pair is never a cross, whatever else the sheet happens
            # to have a column called: only a cross reads a column of legs.
            return PairSpec(name=name, is_cross=False, implied_by=implied_by)

        explicit = cols.get(_norm(name))
        if explicit is not None:
            legs = tuple(str(x).strip().upper() for x in cfg[explicit].dropna())
            if len(legs) != 2:
                data.problems.append(
                    f"cross {name!r} needs exactly 2 legs, found {len(legs)}: {legs}"
                )
                return None
            try:
                infer_leg_signs(name, *legs)
            except ValueError as exc:
                data.problems.append(f"CONFIG column {name!r}: {exc}")
                return None
            return PairSpec(name=name, is_cross=True, legs=legs, implied_by=implied_by)

        try:
            legs = dollar_legs(name)
        except ValueError as exc:
            data.problems.append(f"CONFIG lists {name!r}: {exc}")
            return None
        return PairSpec(name=name, is_cross=True, legs=legs, derived=True,
                        implied_by=implied_by)


    # -- PARAMS -----------------------------------------------------------
    def _load_params(self, xls, data: MarketData) -> None:
        raw = pd.read_excel(xls, "PARAMS", index_col=0)
        row_map: dict[str, int] = {}
        event_rows: list[tuple[int, datetime]] = []
        for i, label in enumerate(raw.index):
            key = PARAM_ROWS.get(_norm(label))
            if key is not None:
                row_map[key] = i
                continue
            when = self._parse_event_label(label)
            if when is not None:
                event_rows.append((i, when))
            elif str(label).strip() and not str(label).startswith("Unnamed"):
                data.problems.append(f"PARAMS row {label!r} is neither a known parameter nor a date")

        for required in ("initial", "long_term"):
            if required not in row_map:
                raise MarketDataError(
                    f"PARAMS sheet has no {required!r} row; found {list(raw.index)}"
                )

        for name, spec in data.pairs.items():
            if name not in raw.columns:
                data.problems.append(f"PARAMS has no column for {name!r}")
                continue
            col = raw[name].values

            def cell(key: str, default: float = 0.0) -> float:
                idx = row_map.get(key)
                if idx is None or idx >= len(col):
                    return default
                v = col[idx]
                return default if v is None or (isinstance(v, float) and math.isnan(v)) else float(v)

            if spec.is_cross:
                # For a cross the first three cells describe the correlation
                # term structure, not a volatility backbone.  Correlations are
                # already dimensionless, so they are not rescaled.
                p = PairParams(
                    name=name,
                    initial=cell("initial"),
                    long_term=cell("long_term"),
                    mean_reversion=cell("mean_reversion", 1.0),
                    short_addon=cell("short_addon") / VOL_POINT,
                    short_decay=cell("short_decay", 50.0),
                )
                for which, v in (("initial", p.initial), ("long_term", p.long_term)):
                    if not -1.0 <= v <= 1.0:
                        data.problems.append(
                            f"{name}: correlation {which} is {v:.4g}, outside [-1, 1] "
                            f"(cross rows use initial/long term/MR as correlation initial/final/decay)"
                        )
            else:
                p = PairParams(
                    name=name,
                    initial=cell("initial") / VOL_POINT,
                    long_term=cell("long_term") / VOL_POINT,
                    mean_reversion=cell("mean_reversion", 5.0),
                    short_addon=cell("short_addon") / VOL_POINT,
                    short_decay=cell("short_decay", 50.0),
                    rate_vol=cell("rate_vol") / VOL_POINT,
                    rate_corr=cell("rate_corr"),
                )
                if p.initial <= 0 or p.long_term <= 0:
                    data.problems.append(
                        f"{name}: initial ({p.initial * VOL_POINT:.4g}) and long term "
                        f"({p.long_term * VOL_POINT:.4g}) volatility must both be positive"
                    )

            shift = timedelta(hours=self.event_tz_offset_hours)
            for idx, when in event_rows:
                if idx >= len(col):
                    continue
                v = col[idx]
                if v is None or (isinstance(v, float) and math.isnan(v)) or float(v) == 0.0:
                    continue
                p.events.append((when - shift, float(v) / VOL_POINT))
            data.params[name] = p

    def _parse_event_label(self, label) -> datetime | None:
        if isinstance(label, datetime):
            return label if label.tzinfo else label.replace(tzinfo=UTC)
        if isinstance(label, pd.Timestamp):
            dt = label.to_pydatetime()
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        try:
            return parse_datetime(str(label))
        except ValueError:
            return None

    # -- per-pair smile sheets --------------------------------------------
    def _load_marks(self, xls, data: MarketData, sheets: set[str]) -> None:
        for name in data.pairs:
            if name not in sheets:
                continue
            df = pd.read_excel(xls, name)
            cols = {_norm(c): c for c in df.columns}
            missing = [k for k in SMILE_COLUMNS if k not in cols]
            if missing:
                data.problems.append(
                    f"sheet {name!r} is missing column(s) {missing}; found {list(df.columns)}"
                )
                continue
            marks: list[SmileMark] = []
            for i, row in df.iterrows():
                tenor = row[cols["expiry"]]
                if tenor is None or (isinstance(tenor, float) and math.isnan(tenor)):
                    continue
                try:
                    values = {
                        field: float(row[cols[label]]) / VOL_POINT
                        for label, field in SMILE_COLUMNS.items() if field != "tenor"
                    }
                except (TypeError, ValueError) as exc:
                    data.problems.append(f"sheet {name!r} row {i + 2}: non-numeric quote ({exc})")
                    continue
                if any(math.isnan(v) for v in values.values()):
                    data.problems.append(f"sheet {name!r} row {i + 2} ({tenor}): blank quote")
                    continue
                if values["st_25"] <= 0 or values["st_10"] <= 0:
                    data.problems.append(
                        f"sheet {name!r} {tenor}: strangles must be positive, got "
                        f"25d={values['st_25'] * VOL_POINT:.4g}, 10d={values['st_10'] * VOL_POINT:.4g}"
                    )
                marks.append(SmileMark(tenor=str(tenor).strip(), **values))
            if marks:
                data.marks[name] = marks
