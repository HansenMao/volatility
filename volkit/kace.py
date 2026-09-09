"""The kACE feed: the marked surface as a ``RATE_FEED`` message.

The desk's pricing platform (kACE) takes its volatilities through an XML
poster page.  Until now the message was built in a spreadsheet: volkit's daily
cumulative export pasted into one sheet, the tenor expiry dates and the wing
marks copied in from Murex, a spread table typed beside them, and three
thousand rows of formulas concatenating it all into ``<node>`` elements to be
copied out again and pasted into the poster.  Every number that sheet needed
is on the loaded book, so this module builds the message from the book and
the sheet goes away.

What a message is, from the sheet that defined it:

* one ``<node>`` per calendar day out to the last pillar, carrying the day's
  ``Maturity`` and an ATM ``Volity`` written ``bid/offer`` -- the cumulative
  (term) volatility to that day's cut, less and plus half a spread;
* then, for every pillar (O/N and the quoted tenors), five nodes at the
  pillar's expiry: the ATM ``bid/offer`` again, the 25d and 10d risk
  reversals (``VolType="RR"``) and the 25d and 10d butterflies
  (``VolType="S"``), each a single value.

All volatilities are decimals (``0.0176`` for 1.76%), dates ``DD MMM YYYY``,
and ``horDate`` is the horizon date the platform is fed as of.

Three things the sheet did are done differently here, on purpose:

* **``horDate`` is the book's valuation date**, not the wall clock.  The sheet
  used ``TODAY()``, so a message built in the evening and sent after midnight
  carried the wrong date silently.
* **The daily series runs to the last pillar**, whatever the daily horizon
  setting says.  The sheet's series stopped at a fixed 1.0 years, and the 1Y
  expiry is 365 to 367 days out depending on the weekday; when it fell past
  the last row the pillar's lookup was ``#N/A``, and the literal text ``#N/A``
  went into the XML.
* **The spread table names the pillars, and a tier names the widths.** The
  ``KACE_SPREADS`` tab of the workbook is a row per tenor and a **column per
  spreading tier** -- ``default`` and whatever else the desk names ("wide",
  "thin", a client tier).  The tenors listed are exactly the pillars posted,
  whichever tier is chosen; the tier decides only how wide the ATM two-way is
  at each of them, and a blank cell in a tier falls back to ``default`` cell
  by cell.  A tenor listed with no mark behind it is refused by name rather
  than defaulted, and so is a tier the tab does not have.

  The table used to be ``pair,tenor,spread``, which tied a width to a
  currency: a desk that wanted to post the same pair twice at two widths had
  nowhere to say so, and a new pair could not be posted until somebody typed
  a whole ladder for it.  The widths are a **quoting policy**, not a property
  of the currency, so they are tiers now and the pair is chosen on the screen
  beside them.

One rule of the sheet's is kept exactly, because it was a rule in disguise: a
day takes the spread of the last pillar whose expiry is on or before it, and
a day before the first pillar's expiry takes the first pillar's spread.  That
was an approximate ``VLOOKUP`` with an ``ISERROR`` fallback; here it is
``spread_for``, and a test pins it.  It is the default and stays the default,
because it is what the sheet posted.  ``interpolate=True`` is the alternative
a desk may ask for: a day between two pillars takes a width read straight
across between theirs, by date, so the two-way widens smoothly instead of
stepping on nine mornings of the year.  It changes only the days *between*
pillars -- a pillar's own width is the tier's whichever rule is in force, and
a day outside the pillars still takes the nearest one's.

The **multiplier** is the other knob the widths take.  A tier is a ladder the
desk maintains on a workbook tab; a morning that wants everything half again
as wide should not have to type a second ladder to say so, and the multiple is
not a policy worth a column.  ``build(..., multiplier=1.5)`` scales every
pillar's width, and it scales the pillar the day-by-day rule then reads, so
what is on the screen, what is in the XML and what the post log records are
the multiplied widths and not the tab's.  It multiplies the *width*, never the
volatility: the mid of every two-way is exactly where it was.

Conventions, as the desk stated them (2026-09-01): a risk reversal is the
base-currency call vol minus the put vol (kACE's *$ call* column for a USD
pair), which is what ``SmileMark.rr_25`` and ``VolSurface.risk_reversal``
both return; ``VolType="S"`` is the butterfly, and the desk's ``ST`` mark is
the number that goes there; O/N is a one-day option and is posted as a
pillar like any other.  O/N has no quoted wings, so under ``source="marks"``
it takes the shortest quoted tenor's, and says so in the notes.

The feed's credentials (the ``username`` / ``password`` in the message header)
are never in this repository: ``--kace-user`` / ``--kace-password``, or
``VOLKIT_KACE_USER`` / ``VOLKIT_KACE_PASSWORD`` in the environment.  A message
with no username is refused rather than built with a blank one, because the
platform would refuse it later with less to say.

Posting (stage 2 of ``claude/kace-export-design.md``) is what the poster
page does when *Send* is pressed, as the desk's own VBA showed it: an HTTP
``POST`` to the kACE server with the message form-encoded as ``xml=...``
(``Content-Type: application/x-www-form-urlencoded``), the reply being the
platform's own ``gfi_message`` with a ``processingTime`` in its header.
``post_message`` does that and nothing else; ``read_reply`` decides whether
what came back says the vols were taken.  The URL is a start-up setting
(``--kace-url`` or ``VOLKIT_KACE_URL``), never typed on the page: a page that
can name the URL can send this server's messages -- credentials included --
anywhere.  Every post, sent or refused, is appended to ``kace_posts.jsonl``
beside the workbook with a hash of the message, so "what did we send kACE
this morning" has an answer that is not somebody's memory.  The network is
injected (``opener``), the way it is in ``dtcc.py``, so all of it is tested
without a kACE to talk to.
"""

from __future__ import annotations

import hashlib
import json
import http.cookiejar
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import paths
from .atm import cut_datetime
from .paths import app_dir
from .timeutil import DAYS_IN_YEAR, UTC, tenor_to_years

#: The workbook tab the spread table is maintained on.
SPREADS_SHEET = "KACE_SPREADS"
#: Where the credentials may come from when they are not on the command line.
ENV_USER = "VOLKIT_KACE_USER"
ENV_PASSWORD = "VOLKIT_KACE_PASSWORD"
#: The scenario the sheet posted into.
DEFAULT_SCENARIO = "Xyz"
#: The tier every other one falls back to, cell by cell, and the one posted
#: when nothing names another.  It is a required column of the tab, so there
#: is always at least one tier and never a table with no widths in it.
DEFAULT_TIER = "default"
#: The tab's own columns, which are not tiers.
FIXED_COLUMNS = ("tenor", "note")
#: The overnight pillar, as the desk spells it.
OVERNIGHT = "O/N"
#: Where the wings may come from.
SOURCES = ("marks", "fitted")
#: Where the message is posted, when it is not on the command line.
ENV_URL = "VOLKIT_KACE_URL"
#: The record of every post, beside the workbook.
POST_LOG_FILENAME = "kace_posts.jsonl"
#: How long to wait for kACE.  The sheet's reply came back in a third of a
#: second; a minute is generous and still a failure somebody sees.
POST_TIMEOUT = 60.0
#: What the poster page sends, exactly.
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
USER_AGENT = "volkit-kace/1.0"
#: Days of daily series past the last pillar, so the last pillar is never the
#: last row and a cut-time rounding cannot push it off the end.
MARGIN_DAYS = 3


class KaceError(ValueError):
    """The message cannot be built, and this says why."""


def canonical_tenor(tenor: str) -> str:
    """``o/n``, ``ON``, ``1d`` and ``O/N`` are one pillar; everything else is upper-cased."""
    t = str(tenor).strip().upper()
    if t in {"O/N", "ON", "1D", "OVERNIGHT"}:
        return OVERNIGHT
    return t


def calendar_tenor(tenor: str) -> str:
    """The tenor the calendar resolves.

    ``O/N`` is one of the short-date codes ``timeutil.parse_tenor`` now reads,
    so this is the identity for everything the spread table can hold; it stays
    as the one place a spread-table pillar is turned into a calendar request,
    because that is the seam where a new spelling would first be needed.
    """
    return tenor


def pillar_years(tenor: str) -> float:
    """Sort key: O/N first, then by approximate year fraction."""
    return tenor_to_years(calendar_tenor(tenor))


# ---------------------------------------------------------------------------
# the spread table
# ---------------------------------------------------------------------------
@dataclass
class SpreadTable:
    """The kACE pillars, and the ATM bid/offer width each spreading tier posts.

    One row per tenor -- those are the pillars -- and one column per **tier**.
    ``tiers`` is what the tab resolves to: a tier's blank cell has already
    taken ``default``'s width, so what a caller reads is a complete ladder
    whichever tier it asked for and there is no second place for the fallback
    to be written differently.
    """

    path: str = ""
    #: tier name -> {tenor: spread}, ``default`` filled in where a tier is blank.
    tiers: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def names(self) -> list[str]:
        """Every tier the tab holds, ``default`` first, then the desk's own."""
        return list(self.tiers)

    @classmethod
    def default_path(cls) -> Path:
        """The workbook whose ``KACE_SPREADS`` tab holds the table."""
        from . import configsheets

        return configsheets.default_workbook()

    @classmethod
    def load(cls, path: str | Path | None = None, *, overlay=None) -> "SpreadTable":
        """Read the ``KACE_SPREADS`` tab.  A tab that is wrong is refused whole.

        ``path`` is the marks workbook: the pillars a message is posted at are
        maintained beside the marks that are posted at them, and one file
        travels to a new machine intact where two did not.
        """
        from . import configsheets

        p = Path(path) if path else cls.default_path()
        table = cls(path=f"{p.name}!{SPREADS_SHEET}")
        try:
            rows = configsheets.read_rows(p, SPREADS_SHEET,
                                          required=("tenor", DEFAULT_TIER),
                                          overlay=overlay)
        except configsheets.ConfigSheetError as exc:
            raise KaceError(f"{exc}{_old_layout_hint(p, overlay)}") from None
        if rows is None:
            raise KaceError(
                f"{p} has no {SPREADS_SHEET!r} tab: a table of one row per tenor -- those are "
                f"the pillars to post -- and one column per spreading tier holding the ATM "
                f"bid/offer width at each, starting with {DEFAULT_TIER!r}")

        # The tiers are whatever columns the tab has grown, in the order it
        # has them, with the fixed ones taken out.  Read off the rows rather
        # than off a list here, because which tiers a desk keeps is the desk's
        # business -- the same reasoning as ``Vega Weights``' pair columns.
        columns = configsheets.columns_for(SPREADS_SHEET, [r.cells for r in rows])
        tiers = [c for c in columns if configsheets.normalise(c) not in FIXED_COLUMNS]
        for name in tiers:
            table.tiers[name] = {}

        bad: list[str] = []
        seen: set[str] = set()
        for row in rows:
            where = f"row {row.number}"
            tenor = canonical_tenor(row.text("tenor"))
            if not tenor:
                bad.append(f"{where}: expected a tenor and a width under {DEFAULT_TIER}")
                continue
            if tenor != OVERNIGHT:
                try:
                    tenor_to_years(tenor)
                except ValueError as exc:
                    bad.append(f"{where}: {exc}")
                    continue
            if tenor in seen:
                bad.append(f"{where}: {tenor} is listed twice")
                continue
            seen.add(tenor)
            widths: dict[str, float] = {}
            for name in tiers:
                try:
                    spread = row.real(name)
                except configsheets.ConfigSheetError:
                    bad.append(f"{where}: the {name} spread for {tenor} is not a number")
                    continue
                if spread is None:
                    continue
                if spread < 0:
                    bad.append(f"{where}: the {name} spread {spread:g} for {tenor} is negative")
                    continue
                widths[name] = spread
            if DEFAULT_TIER not in widths:
                bad.append(f"{where}: {tenor} has no {DEFAULT_TIER} spread; every tenor needs "
                           f"one, because it is what a tier that leaves the cell blank posts")
                continue
            # The fallback happens once, here: a tier is a complete ladder by
            # the time anything reads it.
            for name in tiers:
                table.tiers[name][tenor] = widths.get(name, widths[DEFAULT_TIER])
        if bad:
            raise KaceError(f"{p.name}!{SPREADS_SHEET} could not be read:\n  "
                            + "\n  ".join(bad))
        return table

    def resolve_tier(self, tier: str | None = None) -> str:
        """The tier a request means: the one it named, else ``default``.

        A blank ask is not an error -- the command line and the screen both
        have one -- but a name the tab does not hold is, and it says what the
        tab does hold rather than quietly posting the default's widths under
        another tier's name.
        """
        name = tier_name(tier)
        if not name:
            if DEFAULT_TIER in self.tiers:
                return DEFAULT_TIER
            if not self.tiers:
                raise KaceError(f"{self.path} holds no spreading tiers: it needs a "
                                f"{DEFAULT_TIER!r} column at least")
            return self.names[0]
        if name not in self.tiers:
            raise KaceError(f"{self.path} has no {name!r} tier; it holds "
                            f"{', '.join(self.names)}. A tier is a column of the tab, added "
                            f"in the Config window")
        return name

    def for_tier(self, tier: str | None = None) -> dict[str, float]:
        """One tier's ladder: ``{tenor: spread}``, the pillars and their widths."""
        name = self.resolve_tier(tier)
        rows = self.tiers.get(name) or {}
        if not rows:
            raise KaceError(f"{self.path} has no tenor rows: the tenors listed there are the "
                            f"pillars posted, so a table with none cannot be posted")
        return dict(rows)


def spread_multiplier(value) -> float:
    """What the tier's widths are multiplied by, as a number, or a refusal.

    Blank is 1.0 -- the tab's own ladder, which is the ordinary case and what
    every caller that does not pass one gets.  Anything that is not a positive
    finite number is refused by name rather than quietly taken as 1: a
    multiplier read as a zero posts a two-way with no width at all, and one
    read as a blank posts widths nobody chose.
    """
    if value is None:
        return 1.0
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 1.0
        try:
            value = float(text)
        except ValueError:
            raise KaceError(f"the spread multiplier {text!r} is not a number") from None
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise KaceError(f"the spread multiplier {value!r} is not a number") from None
    if out != out or out in (float("inf"), float("-inf")):
        raise KaceError("the spread multiplier is not a finite number")
    if out <= 0:
        raise KaceError(f"the spread multiplier {out:g} is not positive; a width of zero or "
                        f"less is not a two-way")
    return out


def tier_name(value) -> str:
    """A tier name as the tab's headings are read: lower case, underscores.

    One spelling for the dropdown, the command line and the sheet, so ``Wide``
    typed on one and ``wide`` written on the other are the same tier and not
    two.
    """
    from . import configsheets

    return configsheets.normalise(value) if value is not None else ""


def _old_layout_hint(path: Path, overlay) -> str:
    """Say so when the tab is the old ``pair, tenor, spread`` table.

    The header is not found, so the reader's own message is that the tab has
    no ``tenor, default`` row -- true, and no help at all to a desk looking at
    a tab full of tenors.  This is the one shape it is worth naming, because
    every workbook this tool has ever written has it.
    """
    from . import configsheets

    try:
        if overlay and configsheets.match_sheet(overlay, SPREADS_SHEET) is not None:
            return ""
        rows = configsheets.read_rows(path, SPREADS_SHEET, required=("pair", "tenor", "spread"))
    except (configsheets.ConfigSheetError, OSError):
        return ""
    if rows is None:
        return ""
    return (f". It is the old 'pair, tenor, spread' layout: the widths are not tied to a "
            f"currency any more. Replace the header with 'tenor, {DEFAULT_TIER}' and one "
            f"column per spreading tier, one row per pillar -- the pair is chosen on the "
            f"kACE feed screen beside the tier")


# ---------------------------------------------------------------------------
# the message
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pillar:
    """One tenor the platform is fed wings at.  Volatilities in vol points."""

    tenor: str
    expiry: date
    spread: float
    atm: float
    rr25: float
    rr10: float
    fly25: float
    fly10: float
    #: Where the wings came from: ``marks``, ``fitted``, or the tenor they were borrowed from.
    wings: str

    @property
    def bid(self) -> float:
        return self.atm - self.spread / 2.0

    @property
    def offer(self) -> float:
        return self.atm + self.spread / 2.0


@dataclass
class Feed:
    """Everything a ``RATE_FEED`` message for one pair holds, before it is XML."""

    pair: str
    hor_date: date
    cut: str
    source: str
    daily: dict[date, float]               # cumulative vol to the day's cut, vol points
    pillars: list[Pillar]
    #: The spreading tier the widths came from.  On the message only as the
    #: widths themselves; carried here so the screen, the file name and the
    #: post log can all say which policy was posted.
    tier: str = DEFAULT_TIER
    #: What the tier's widths were multiplied by on the way in.  The pillars
    #: already hold the multiplied width -- this is here so the screen and the
    #: log can say the widths are not the tab's.
    multiplier: float = 1.0
    #: Whether a day between two pillars takes a width read across between
    #: them (``True``) or the sheet's step rule (``False``, the default).
    interpolate: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ccy(self) -> str:
        return self.pair[:3].upper()

    @property
    def ctr(self) -> str:
        return self.pair[3:6].upper()

    def node_count(self) -> int:
        return len(self.daily) + 5 * len(self.pillars)

    def summary(self) -> dict:
        days = sorted(self.daily)
        return {
            "pair": self.pair, "hor_date": self.hor_date.isoformat(), "cut": self.cut,
            "source": self.source, "tier": self.tier,
            "multiplier": self.multiplier, "interpolate": self.interpolate,
            "days": len(days),
            "first_day": days[0].isoformat() if days else None,
            "last_day": days[-1].isoformat() if days else None,
            "nodes": self.node_count(),
            "pillars": [{"tenor": p.tenor, "expiry": p.expiry.isoformat(), "spread": p.spread,
                         "atm": p.atm, "bid": p.bid, "offer": p.offer,
                         "rr25": p.rr25, "rr10": p.rr10, "fly25": p.fly25, "fly10": p.fly10,
                         "wings": p.wings} for p in self.pillars],
            "notes": list(self.notes),
        }

    def xml(self, username: str, password: str, *, scenario: str = DEFAULT_SCENARIO,
            timestamp: datetime | None = None, transaction_id: str = "1234567890") -> str:
        """The message, as the poster page takes it."""
        _require_credentials(username)
        lines = _header(username, password, timestamp, transaction_id)
        lines += _action(scenario, self.hor_date, clear=False)
        lines.append('    <data name="data1" format="NAME_VALUE">')
        pillars = sorted(self.pillars, key=lambda p: p.expiry)
        n = 0
        for day, vol in sorted(self.daily.items()):
            n += 1
            half = spread_for(day, pillars, interpolate=self.interpolate) / 2.0
            lines += _node(str(n), self.ccy, self.ctr, day, [
                ("VolType", "ATM"), ("Volity", _bid_offer(vol - half, vol + half, day))])
        s = 0
        for p in pillars:
            s += 1
            lines += _node(f"S{s}", self.ccy, self.ctr, p.expiry, [
                ("VolType", "ATM"), ("Volity", _bid_offer(p.bid, p.offer, p.expiry))])
            for pct, kind, value in (("0.25", "RR", p.rr25), ("0.10", "RR", p.rr10),
                                     ("0.25", "S", p.fly25), ("0.10", "S", p.fly10)):
                s += 1
                lines += _node(f"S{s}", self.ccy, self.ctr, p.expiry, [
                    ("PctDelta", pct), ("VolType", kind), ("Volity", _decimal(value / 100.0))])
        lines += ['    </data>', '  </body>', '</gfi_message>']
        return "\n".join(lines) + "\n"


def clear_message(pair: str, hor_date: date, username: str, password: str, *,
                  scenario: str = DEFAULT_SCENARIO, timestamp: datetime | None = None,
                  transaction_id: str = "1234567890") -> str:
    """The message that wipes a pair's volatilities from the scenario."""
    _require_credentials(username)
    ccy, ctr = pair[:3].upper(), pair[3:6].upper()
    lines = _header(username, password, timestamp, transaction_id)
    lines += _action(scenario, hor_date, clear=True)
    lines += ['    <data name="data1" format="NAME_VALUE">',
              f'      <node name="{ccy}{ctr}">',
              '        <field name="RateType" value="Volatility"/>',
              f'        <field name="Currency" value="{ccy}"/>',
              f'        <field name="CtrCcy" value="{ctr}"/>',
              '      </node>',
              '    </data>', '  </body>', '</gfi_message>']
    return "\n".join(lines) + "\n"


def read_ladder(points, x: float, *, interpolate: bool = False) -> float:
    """One width off a ladder of ``(position, width)`` pairs, at ``x``.

    The one place the rule lives, so the message and every other reader of a
    spreading tier cannot answer the same question two ways.  ``points`` is
    whatever the caller can measure a maturity along -- an ordinal date for
    the daily series, a year fraction for a tenor ladder -- and the rule is
    the same in both:

    * stepped (the default): the last rung at or before ``x``, and the first
      rung's width for anything before it.  That is the sheet's approximate
      ``VLOOKUP`` and its ``ISERROR`` fallback, and it is what was posted.
    * interpolated: read straight across between the two rungs ``x`` falls
      between.  Outside the ladder there is nothing to read across to and the
      nearest rung's width is taken, which is the answer the step rule gives
      at both ends; a rung's own position is its own width under either rule.

    ``points`` must be non-empty.  Order does not matter -- it is sorted here
    -- and two rungs at one position resolve to the later one, because a
    ladder cannot be read across a gap of nothing.
    """
    ordered = sorted(points)
    if not ordered:
        raise KaceError("a width cannot be read off an empty ladder")
    if not interpolate:
        chosen = ordered[0][1]
        for at, width in ordered:
            if at <= x:
                chosen = width
            else:
                break
        return chosen
    if x <= ordered[0][0]:
        return ordered[0][1]
    for (lo_at, lo_w), (hi_at, hi_w) in zip(ordered, ordered[1:]):
        if x <= hi_at:
            span = hi_at - lo_at
            if span <= 0:                      # two rungs at one position
                return hi_w
            return lo_w + (hi_w - lo_w) * ((x - lo_at) / span)
    return ordered[-1][1]


def spread_for(day: date, pillars: list[Pillar], *, interpolate: bool = False) -> float:
    """The width posted on one day of the daily series, off the pillars.

    ``read_ladder`` along the pillars' own expiry dates.  Whole days are the
    right resolution here: a pillar expires on a date and the series has one
    node per date, so there is nothing finer to interpolate over.
    """
    return read_ladder([(p.expiry.toordinal(), p.spread) for p in pillars],
                       day.toordinal(), interpolate=interpolate)


def width_at(widths: dict[str, float], years: float, *,
             multiplier: float | str | None = None,
             interpolate: bool = False) -> float | None:
    """A spreading tier's width at an arbitrary maturity, in volatility points.

    The tier is a ladder of tenors, so the position along it is the tenor's
    year fraction (``pillar_years``) and the maturity asked for is a year
    fraction too.  This is what a screen that is not the feed reads a tier
    with: the market-maker panel's fallback width asks for 47 days and the
    tab holds 1M and 2M, and the answer is the same rule the message uses
    between two pillars -- stepped unless told otherwise.

    ``None`` for an empty ladder, which is a tier that cannot answer rather
    than a width of zero.  The multiplier is the feed's, validated the same
    way, so a tier scaled on one screen means the same thing on the other.
    """
    if not widths:
        return None
    factor = spread_multiplier(multiplier)
    points = [(pillar_years(t), w) for t, w in widths.items()]
    return read_ladder(points, float(years), interpolate=interpolate) * factor


def credentials(user: str | None = None, password: str | None = None) -> tuple[str, str]:
    """What was given, else what the environment holds; blank when neither."""
    return (user if user is not None else os.environ.get(ENV_USER, ""),
            password if password is not None else os.environ.get(ENV_PASSWORD, ""))


# ---------------------------------------------------------------------------
# from the book
# ---------------------------------------------------------------------------
def build(book, pair: str, spreads: SpreadTable, *, tier: str | None = None,
          cut: str = "NY", source: str = "marks", method: str = "SVI",
          multiplier: float | str | None = None, interpolate: bool = False) -> Feed:
    """The feed for one pair at one spreading tier, off the book as it is marked now."""
    pair = pair.upper()
    if source not in SOURCES:
        raise KaceError(f"unknown wing source {source!r}; expected one of {SOURCES}")
    surface = book[pair]
    today = book.clock.now.date()
    chosen = spreads.resolve_tier(tier)
    factor = spread_multiplier(multiplier)
    widths = {t: w * factor for t, w in spreads.for_tier(chosen).items()}
    marks = {canonical_tenor(m.tenor): m for m in surface.marks}
    notes: list[str] = []
    if factor != 1.0:
        notes.append(f"the ATM widths are the {chosen} tier's multiplied by {factor:g}; "
                     f"the mid of every two-way is where it was")
    if interpolate:
        notes.append("a day between two pillars takes a width read across between them "
                     "rather than the nearer pillar's")

    # The pillars are the spread table's tenors, in expiry order.  Each needs
    # a mark behind it, except O/N, which borrows the shortest quoted wings.
    tenors = sorted(widths, key=pillar_years)
    unmarked = [t for t in tenors if t != OVERNIGHT and t not in marks]
    if unmarked:
        raise KaceError(f"{spreads.path} lists {', '.join(unmarked)}, but the {pair} sheet "
                        f"quotes no wings there (it has {', '.join(sorted(marks, key=pillar_years))}); "
                        f"a pillar with no mark behind it cannot be posted. A tenor the pair sheet "
                        f"quotes and CONFIG's TENORS column does not list is not read, so check "
                        f"that too; --wings fitted posts the smile's own wings at any pillar")
    quoted = [t for t in tenors if t != OVERNIGHT]
    if not quoted:
        raise KaceError(f"{spreads.path} lists only O/N; at least one quoted tenor "
                        f"is needed to carry the wings")
    expiries = {t: book.calendars.expiry_date(pair, calendar_tenor(t), today) for t in tenors}

    # The daily series runs to the last pillar, whatever the horizon setting
    # says -- the pillar has to be a row of it.
    last = max(expiries.values())
    horizon = ((last - today).days + MARGIN_DAYS) / DAYS_IN_YEAR
    series = surface.atm.daily_series(horizon, cut)
    daily: dict[date, float] = {}
    for label, v in series.items():
        day = datetime.strptime(label, "%Y/%m/%d").date()
        if not v["cumulative_defined"]:
            notes.append(f"{day:%d %b %Y} is the current quoting day, with no whole volatility "
                         f"day to normalise by; it is not posted")
            continue
        daily[day] = v["cumulative"] * 100.0
    if not daily:
        raise KaceError(f"the daily series for {pair} is empty")
    missing = [t for t in tenors if expiries[t] not in daily]
    if missing:
        raise KaceError(f"the daily series does not reach the {', '.join(missing)} expiry "
                        f"({', '.join(expiries[t].isoformat() for t in missing)}); the last "
                        f"day it holds is {max(daily):%Y-%m-%d}")

    pillars: list[Pillar] = []
    for t in tenors:
        expiry = expiries[t]
        if source == "marks":
            src = t if t in marks else quoted[0]
            m = marks[src]
            wings = (m.rr_25 * 100.0, m.rr_10 * 100.0, m.st_25 * 100.0, m.st_10 * 100.0)
            origin = "marks" if src == t else f"marks at {src}"
            if src != t:
                notes.append(f"{t} has no quoted wings; the {src} marks are posted there")
        else:
            when = cut_datetime(datetime.combine(expiry, datetime.min.time()).replace(tzinfo=UTC),
                                cut, surface.atm.dst_aware_cuts)
            wings = (surface.risk_reversal(when, 0.25, method, cut) * 100.0,
                     surface.risk_reversal(when, 0.10, method, cut) * 100.0,
                     surface.strangle(when, 0.25, method, cut) * 100.0,
                     surface.strangle(when, 0.10, method, cut) * 100.0)
            origin = "fitted"
        pillars.append(Pillar(tenor=t, expiry=expiry, spread=widths[t], atm=daily[expiry],
                              rr25=wings[0], rr10=wings[1], fly25=wings[2], fly10=wings[3],
                              wings=origin))
    return Feed(pair=pair, hor_date=today, cut=cut.upper(), source=source,
                daily=daily, pillars=pillars, tier=chosen, multiplier=factor,
                interpolate=bool(interpolate), notes=notes)


# ---------------------------------------------------------------------------
# XML pieces
# ---------------------------------------------------------------------------
def _require_credentials(username: str) -> None:
    if not username:
        raise KaceError(f"no kACE username: pass --kace-user (and --kace-password), or set "
                        f"{ENV_USER} and {ENV_PASSWORD}; the message header carries them and "
                        f"the platform refuses a message without them")


def _stamp(timestamp: datetime | None) -> str:
    ts = timestamp or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    s = ts.strftime("%Y-%m-%dT%H:%M:%S%z")
    return s[:-2] + ":" + s[-2:]


def _header(username: str, password: str, timestamp: datetime | None, transaction_id: str) -> list[str]:
    return ['<?xml version="1.0" encoding="UTF-8"?>',
            '<gfi_message version="2.0">',
            '  <header>',
            f'    <transactionId>{escape(str(transaction_id))}</transactionId>',
            f'    <timestamp>{_stamp(timestamp)}</timestamp>',
            f'    <username>{escape(username)}</username>',
            f'    <password>{escape(password)}</password>',
            '  </header>',
            '  <body>']


def _action(scenario: str, hor_date: date, *, clear: bool) -> list[str]:
    lines = ['    <action name="action1" function="RATE_FEED" version="1.0">',
             '      <option name="data" ref="data1"/>',
             f'      <option name="scenario" value="{_attr(scenario)}"/>',
             f'      <option name="horDate" value="{_date(hor_date)}"/>']
    if clear:
        lines.append('      <option name="clearRate" value="true"/>')
    lines.append('    </action>')
    return lines


def _node(name: str, ccy: str, ctr: str, maturity: date, fields: list[tuple[str, str]]) -> list[str]:
    out = [f'      <node name="{name}">',
           '        <field name="RateType" value="Volatility"/>',
           f'        <field name="Currency" value="{ccy}"/>',
           f'        <field name="CtrCcy" value="{ctr}"/>',
           f'        <field name="Maturity" value="{_date(maturity)}"/>']
    out += [f'        <field name="{_attr(k)}" value="{_attr(v)}"/>' for k, v in fields]
    out.append('      </node>')
    return out


def _attr(s: str) -> str:
    return escape(str(s), {'"': "&quot;"})


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _date(d: date) -> str:
    """``DD MMM YYYY`` in English whatever the machine's locale, which ``%b`` is not."""
    return f"{d.day:02d} {_MONTHS[d.month - 1]} {d.year}"


def _decimal(x: float) -> str:
    """A plain decimal: no exponent, the sheet's fifteen significant figures."""
    s = f"{x:.15g}"
    if "e" in s or "E" in s:
        s = f"{x:.20f}".rstrip("0").rstrip(".")
    return s


def _bid_offer(bid: float, offer: float, day: date) -> str:
    if bid <= 0:
        raise KaceError(f"the ATM bid on {day:%d %b %Y} is {bid:.4f} vol points: the spread is "
                        f"wider than twice the volatility, and the platform will not take a "
                        f"non-positive bid")
    return f"{_decimal(bid / 100.0)}/{_decimal(offer / 100.0)}"


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------
class KacePostError(KaceError):
    """The message could not be delivered: nothing answered, or not kACE."""


@dataclass
class PostResult:
    """What one post came to."""

    url: str
    ok: bool
    status: int
    message: str                      # one sentence a person reads
    processing_time: float | None = None
    reply: str = ""                   # the reply's text, trimmed
    bytes_sent: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def form_body(xml_text: str) -> bytes:
    """``xml=<url-encoded message>``, as the poster page and the desk's VBA send it.

    Percent-encoded throughout -- a space is ``%20``, never ``+``.  That is
    what the VBA's ``URLEncode`` produces and therefore the only spelling the
    platform is known to accept; ``urlencode`` would send ``+``, which is
    correct form encoding but not what has ever been shown to work here.
    """
    return ("xml=" + urllib.parse.quote(xml_text, safe="")).encode("ascii")


def message_hash(xml_text: str) -> str:
    return hashlib.sha256(xml_text.encode("utf-8")).hexdigest()[:16]


def settings(url: str | None = None) -> str:
    """The post URL: what was given, else the environment, else blank."""
    return url if url is not None else os.environ.get(ENV_URL, "")


def read_reply(text: str) -> tuple[bool, float | None, str]:
    """Whether kACE took the message, from what it sent back.

    The reply the poster page shows is a ``gfi_message`` whose header carries
    a ``processingTime`` and whose body carries a ``<response>`` for the
    action.  That is the one shape known to mean success.  Anything else --
    a page that is not XML (a login page, a proxy's HTML), a message with no
    response, or one carrying an element or attribute that says *error* --
    is reported as not taken, with the reply's first line, because the
    platform's failure vocabulary has not been seen yet and a guess that
    read a refusal as a success would be the worst outcome here.
    """
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    try:
        root = ET.fromstring(text.strip().encode("utf-8"))
    except ET.ParseError:
        head = text[:2000].lstrip().lower()
        if head.startswith("<!doctype html") or "<html" in head:
            title = _page_title(text)
            return False, None, (
                "the reply is a web page, not a kACE message"
                + (f': "{title}"' if title else "")
                + " -- that address served the site rather than the feed gateway, or "
                  "sent us to its login page")
        return False, None, f"the reply is not XML: {first[:160] or '(empty)'}"
    if root.tag != "gfi_message":
        if root.tag.lower() == "html":   # well-formed enough to parse, still a page
            title = _page_title(text)
            return False, None, (
                "the reply is a web page, not a kACE message"
                + (f': "{title}"' if title else "")
                + " -- that address served the site rather than the feed gateway, or "
                  "sent us to its login page")
        return False, None, f"the reply is not a gfi_message: <{root.tag}>"
    took = None
    header = root.find("header")
    if header is not None:
        pt = header.findtext("processingTime")
        if pt:
            try:
                took = float(pt)
            except ValueError:
                took = None
    errors: list[str] = []
    for el in root.iter():
        if "error" in el.tag.lower() or "fault" in el.tag.lower():
            errors.append((el.text or "").strip() or el.tag)
        for k, v in el.attrib.items():
            if "error" in k.lower() or (k.lower() in {"status", "result"} and "error" in v.lower()):
                errors.append(f"{k}={v}")
    if errors:
        return False, took, "kACE reported: " + "; ".join(errors)[:300]
    body = root.find("body")
    if body is None or body.find("response") is None:
        return False, took, f"the reply carries no <response>: {first[:160]}"
    return True, took, (f"kACE took the message in {took:.3f}s" if took is not None
                        else "kACE took the message")


def _page_title(text: str) -> str:
    """The ``<title>`` of an HTML reply, collapsed to one line, or ``''``."""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    return " ".join(m.group(1).split())[:120] if m else ""


def _https_context(ca: str | None, insecure: bool) -> ssl.SSLContext:
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    if ca:
        ctx.load_verify_locations(cafile=ca)
    return ctx


#: The TLS handshakes tried, in order, against a server whose TLS is older than
#: the OpenSSL this Python ships with will accept by default.
#:
#: kACE answers with ``[SSL: DH_KEY_TOO_SMALL] dh key too small``: it offers a
#: 1024-bit Diffie-Hellman group for key exchange, and OpenSSL 3 refuses to
#: talk to one at its default security level.  The poster web page never sees
#: this because browsers dropped finite-field DH years ago and the server then
#: negotiates ECDHE or plain RSA with them instead.  So the first fallback is
#: to do what a browser does -- leave DH out of the offer, which changes
#: nothing about certificate checking -- and only if the server has *nothing*
#: else does the last resort lower OpenSSL's bar to what the server can do.
#: Whichever step works is remembered per host and reported with the post,
#: so a desk knows exactly how it is talking to its pricing platform.
_TLS_STEPS: tuple[tuple[str, bool, bool], ...] = (   # (label, drop DHE, security level 0)
    ("", False, False),
    ("without finite-field Diffie-Hellman, as a browser would", True, False),
    ("at OpenSSL security level 0 (the server's TLS is old)", False, True),
)
_BROWSER_RSA_SUITES = ("AES128-GCM-SHA256", "AES256-GCM-SHA384", "AES128-SHA", "AES256-SHA")
_TLS_STEP_BY_HOST: dict[str, int] = {}
_TLS_NOTE_BY_HOST: dict[str, str] = {}
_LEGACY_TLS_SIGNS = ("DH_KEY_TOO_SMALL", "DH KEY TOO SMALL", "HANDSHAKE_FAILURE",
                     "NO_SHARED_CIPHER", "NO_CIPHERS_AVAILABLE", "UNSUPPORTED_PROTOCOL",
                     "WRONG_SSL_VERSION", "PROTOCOL_VERSION", "WRONG_VERSION_NUMBER",
                     "EE_KEY_TOO_SMALL", "CA_KEY_TOO_SMALL", "CA_MD_TOO_WEAK")


def _tls_context(ca: str | None, insecure: bool, step: int) -> ssl.SSLContext:
    ctx = _https_context(ca, insecure)
    _, drop_dhe, level0 = _TLS_STEPS[step]
    if not (drop_dhe or level0):
        return ctx
    # Python's own hardened cipher list, edited -- not OpenSSL's wider DEFAULT.
    names = [c["name"] for c in ctx.get_ciphers()
             if not (drop_dhe and c["name"].startswith("DHE-"))]
    if drop_dhe:
        # ...plus the RSA key-exchange suites a browser still offers, which is
        # what a server with nothing but old DH and RSA agrees with a browser.
        names += _BROWSER_RSA_SUITES
    ctx.set_ciphers(":".join(names) + (":@SECLEVEL=0" if level0 else ""))
    if level0:
        # an old server may also be a TLS 1.0/1.1 server
        ctx.minimum_version = ssl.TLSVersion.TLSv1
    return ctx


def _is_legacy_tls(reason: str) -> bool:
    text = reason.upper()
    return any(sign in text for sign in _LEGACY_TLS_SIGNS)


def tls_note(url: str) -> str:
    """How the last post to ``url``'s host had to talk TLS, or '' if normally."""
    return _TLS_NOTE_BY_HOST.get(urllib.parse.urlsplit(url).netloc.lower(), "")


#: Cookies the platform sets, kept for the life of the process so a session
#: picked up on one post is carried by the next.
_COOKIES = http.cookiejar.CookieJar()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect on a post.

    urllib's default handler follows a 302 and re-issues it as a GET, so a
    platform that answers "log in first" arrives here as a clean 200 carrying
    a login page and the redirect is invisible.  Refusing to follow turns that
    back into what it is: a 302, and the address it pointed at.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _post_once(url: str, body: bytes, headers: dict, *, timeout: float,
               context: ssl.SSLContext | None) -> tuple[int, bytes]:
    """One POST through urllib; the bit the fallback loop below wraps."""
    handlers = [urllib.request.ProxyHandler({}), _NoRedirect(),
                urllib.request.HTTPCookieProcessor(_COOKIES)]
    if context is not None:
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as reply:
            return getattr(reply, "status", 200), reply.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            where = exc.headers.get("Location", "(no Location header)")
            return exc.code, (
                f"the server did not answer the post: it redirected to {where}. "
                f"That is the platform asking for a session this post does not carry -- "
                f"the page login puts a cookie in the browser and the post needs the same one."
            ).encode("utf-8")
        return exc.code, exc.read()[:8192]


def http_post(url: str, body: bytes, headers: dict, *, timeout: float,
              ca: str | None = None, insecure: bool = False) -> tuple[int, bytes]:
    """The real network: one POST, no proxy.  Replaced wholesale in tests.

    No proxy on purpose: the platform is on the desk's own network, and the
    corporate proxy the DTCC download goes out through is exactly the thing
    that must not see this request.  An empty ``ProxyHandler`` is what stops
    urllib installing one from the environment or the Windows registry.

    Over HTTPS the handshake is tried the ordinary way first and then down
    ``_TLS_STEPS`` when the server's TLS is what refuses it; the step that
    worked is kept for the host so later posts do not re-learn it.
    """
    https = url.lower().startswith("https:")
    host = urllib.parse.urlsplit(url).netloc.lower()
    start = _TLS_STEP_BY_HOST.get(host, 0) if https else 0
    steps = range(start, len(_TLS_STEPS)) if https else range(1)
    tried: list[str] = []
    for step in steps:
        context = _tls_context(ca, insecure, step) if https else None
        try:
            status, raw = _post_once(url, body, headers, timeout=timeout, context=context)
        except urllib.error.URLError as exc:
            reason = str(exc.reason)
            if https and _is_legacy_tls(reason) and step + 1 < len(_TLS_STEPS):
                tried.append(reason)
                continue
            hint = ""
            if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate" in reason.lower():
                hint = (" -- the server's certificate is not one this machine trusts; name "
                        "the desk's CA bundle with --kace-ca, or --kace-insecure to post "
                        "without checking it")
            elif tried:
                hint = (" -- after also trying " + " and ".join(
                    _TLS_STEPS[i][0] for i in range(start + 1, step + 1)))
            raise KacePostError(f"could not reach {url} (directly, through no proxy): "
                                f"{reason}{hint}") from None
        except (TimeoutError, OSError) as exc:
            raise KacePostError(f"could not reach {url}: {exc}") from None
        if https:
            _TLS_STEP_BY_HOST[host] = step
            _TLS_NOTE_BY_HOST[host] = (f"TLS {_TLS_STEPS[step][0]}" if step else "")
        return status, raw
    raise KacePostError(f"could not reach {url}")  # pragma: no cover


def post_message(xml_text: str, url: str, *, opener=None, timeout: float = POST_TIMEOUT,
                 ca: str | None = None, insecure: bool = False) -> PostResult:
    """Send one message the way the poster page does, and read what came back."""
    url = (url or "").strip()
    if not url:
        raise KacePostError(f"no kACE URL: pass --kace-url (or set {ENV_URL}); it is the "
                            f"address the XML poster page itself posts to, e.g. "
                            f"https://host:8500/... -- never typed on the page")
    if not url.lower().startswith(("http://", "https://")):
        raise KacePostError(f"the kACE URL {url!r} is not an http(s) address")
    body = form_body(xml_text)
    headers = {"Content-Type": FORM_CONTENT_TYPE, "User-Agent": USER_AGENT,
               "Content-Length": str(len(body))}
    send = opener or http_post
    status, raw = send(url, body, headers, timeout=timeout, ca=ca, insecure=insecure)
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if status != 200:
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        return PostResult(url=url, ok=False, status=status, bytes_sent=len(body),
                          message=f"HTTP {status} from {url}: {first[:160] or '(no body)'}",
                          reply=text[:4000])
    ok, took, message = read_reply(text)
    note = tls_note(url) if send is http_post else ""
    if note:
        message += f" ({note})"
    return PostResult(url=url, ok=ok, status=status, processing_time=took, message=message,
                      reply=text[:4000], bytes_sent=len(body))


@dataclass
class PostLog:
    """Every post, one JSON line each, beside the workbook."""

    path: str = ""

    @classmethod
    def default_path(cls) -> Path:
        return app_dir() / POST_LOG_FILENAME

    @classmethod
    def at(cls, path: str | Path | None = None) -> "PostLog":
        return cls(path=str(Path(path) if path else cls.default_path()))

    def record(self, entry: dict) -> dict:
        entry = dict(entry)
        with Path(self.path).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
        return entry

    def entries(self, pair: str | None = None, limit: int = 20) -> list[dict]:
        """The last ``limit`` entries, newest last; a line that will not parse is skipped."""
        p = Path(self.path)
        if not p.exists():
            return []
        out = []
        for line in paths.read_text(p).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if pair and str(row.get("pair", "")).upper() != pair.upper():
                continue
            out.append(row)
        return out[-limit:]


def post_feed(xml_text: str, *, pair: str, scenario: str, clear: bool, hor_date: date,
              nodes: int, url: str, log: PostLog | None, when: datetime, opener=None,
              ca: str | None = None, insecure: bool = False, dry_run: bool = False,
              tier: str = "", multiplier: float = 1.0, interpolate: bool = False) -> dict:
    """Post one message and write the record; the record is the return value.

    A refused post is recorded too -- with what refused it -- because the
    question the log answers is "what happened this morning", and "nothing
    reached kACE" is an answer.  A dry run records nothing and sends
    nothing: it says what *would* go, and where.
    """
    entry = {"at": when.isoformat(timespec="seconds"), "pair": pair.upper(),
             "scenario": scenario, "clear": bool(clear), "hor_date": hor_date.isoformat(),
             # Which spreading tier's widths went out.  A clear carries none,
             # and the log says so rather than naming a tier nothing used.
             "tier": tier or "",
             # And what was done to them on the way out: the multiple the tier
             # was scaled by, and whether the days between pillars were read
             # across or stepped.  A tier alone no longer says what went.
             "multiplier": float(multiplier or 1.0),
             "interpolate": bool(interpolate),
             "nodes": nodes, "hash": message_hash(xml_text), "bytes": len(form_body(xml_text)),
             "url": url or ""}
    if dry_run:
        entry.update({"dry_run": True, "ok": None,
                      "message": f"dry run: {entry['bytes']} bytes would be posted to "
                                 f"{url or '(no URL set)'}"})
        return entry
    try:
        result = post_message(xml_text, url, opener=opener, ca=ca, insecure=insecure)
        entry.update({"ok": result.ok, "status": result.status, "message": result.message,
                      "processing_time": result.processing_time, "reply": result.reply[:4000]})
    except KacePostError as exc:
        entry.update({"ok": False, "status": None, "message": str(exc)})
    if log is not None:
        try:
            log.record(entry)
            entry["logged"] = log.path
        except OSError as exc:
            entry["logged"] = None
            entry["message"] += f" (and the post log at {log.path} could not be written: {exc})"
    return entry
