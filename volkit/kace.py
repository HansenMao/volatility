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
* **The spread table names the pillars.** ``kace_spreads.csv`` beside the
  workbook holds ``pair,tenor,spread`` rows, and the tenors listed for a pair
  are exactly the pillars posted.  A tenor listed with no mark behind it, or
  a pair with no rows, is refused by name rather than defaulted.

One rule of the sheet's is kept exactly, because it was a rule in disguise: a
day takes the spread of the last pillar whose expiry is on or before it, and
a day before the first pillar's expiry takes the first pillar's spread.  That
was an approximate ``VLOOKUP`` with an ``ISERROR`` fallback; here it is
``spread_for``, and a test pins it.

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

import csv
import hashlib
import json
import os
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
from .paths import app_dir, find_data_file
from .timeutil import DAYS_IN_YEAR, UTC, tenor_to_years

#: The spread table, beside the workbook.
SPREADS_FILENAME = "kace_spreads.csv"
#: Where the credentials may come from when they are not on the command line.
ENV_USER = "VOLKIT_KACE_USER"
ENV_PASSWORD = "VOLKIT_KACE_PASSWORD"
#: The scenario the sheet posted into.
DEFAULT_SCENARIO = "Xyz"
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
    """``pair,tenor,spread`` rows: the ATM bid/offer width, and the pillars, per pair."""

    path: str = ""
    rows: dict[str, dict[str, float]] = field(default_factory=dict)

    @classmethod
    def default_path(cls) -> Path:
        """Beside the exe, or ``files/`` in a source tree; the first that exists."""
        found = find_data_file(SPREADS_FILENAME, f"files/{SPREADS_FILENAME}")
        return found if found is not None else app_dir() / SPREADS_FILENAME

    @classmethod
    def load(cls, path: str | Path | None = None) -> "SpreadTable":
        """Read the table.  A file that is there and wrong is refused whole."""
        p = Path(path) if path else cls.default_path()
        if not p.exists():
            raise KaceError(f"no kACE spread table at {p}: a 'pair,tenor,spread' CSV naming "
                            f"the pillars to post and the ATM bid/offer width at each")
        table = cls(path=str(p))
        bad: list[str] = []
        with paths.open_text(p, newline="") as fh:
            for n, raw in enumerate(csv.reader(fh), start=1):
                cells = [c.strip() for c in raw]
                if not cells or not any(cells) or cells[0].startswith("#"):
                    continue
                if cells[0].lower() == "pair" and len(cells) > 1 and cells[1].lower() == "tenor":
                    continue
                if len(cells) < 3:
                    bad.append(f"line {n}: expected 'pair,tenor,spread', got {','.join(cells)!r}")
                    continue
                pair, tenor = cells[0].upper(), canonical_tenor(cells[1])
                try:
                    spread = float(cells[2])
                except ValueError:
                    bad.append(f"line {n}: spread {cells[2]!r} for {pair} {tenor} is not a number")
                    continue
                if spread < 0:
                    bad.append(f"line {n}: spread {spread:g} for {pair} {tenor} is negative")
                    continue
                if tenor != OVERNIGHT:
                    try:
                        tenor_to_years(tenor)
                    except ValueError as exc:
                        bad.append(f"line {n}: {exc}")
                        continue
                if tenor in table.rows.setdefault(pair, {}):
                    bad.append(f"line {n}: {pair} {tenor} is listed twice")
                    continue
                table.rows[pair][tenor] = spread
        if bad:
            raise KaceError(f"{p} could not be read:\n  " + "\n  ".join(bad))
        return table

    def for_pair(self, pair: str) -> dict[str, float]:
        rows = self.rows.get(pair.upper())
        if not rows:
            raise KaceError(f"{self.path} has no rows for {pair.upper()}: the tenors listed there "
                            f"are the pillars posted, so a pair with none cannot be posted")
        return dict(rows)


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
            "source": self.source, "days": len(days),
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
            half = spread_for(day, pillars) / 2.0
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


def spread_for(day: date, pillars: list[Pillar]) -> float:
    """The sheet's rule: the last pillar expiring on or before the day, else the first."""
    ordered = sorted(pillars, key=lambda p: p.expiry)
    chosen = ordered[0].spread
    for p in ordered:
        if p.expiry <= day:
            chosen = p.spread
        else:
            break
    return chosen


def credentials(user: str | None = None, password: str | None = None) -> tuple[str, str]:
    """What was given, else what the environment holds; blank when neither."""
    return (user if user is not None else os.environ.get(ENV_USER, ""),
            password if password is not None else os.environ.get(ENV_PASSWORD, ""))


# ---------------------------------------------------------------------------
# from the book
# ---------------------------------------------------------------------------
def build(book, pair: str, spreads: SpreadTable, *, cut: str = "NY", source: str = "marks",
          method: str = "SVI") -> Feed:
    """The feed for one pair, off the book as it is marked now."""
    pair = pair.upper()
    if source not in SOURCES:
        raise KaceError(f"unknown wing source {source!r}; expected one of {SOURCES}")
    surface = book[pair]
    today = book.clock.now.date()
    widths = spreads.for_pair(pair)
    marks = {canonical_tenor(m.tenor): m for m in surface.marks}
    notes: list[str] = []

    # The pillars are the spread table's tenors, in expiry order.  Each needs
    # a mark behind it, except O/N, which borrows the shortest quoted wings.
    tenors = sorted(widths, key=pillar_years)
    unmarked = [t for t in tenors if t != OVERNIGHT and t not in marks]
    if unmarked:
        raise KaceError(f"{spreads.path} lists {', '.join(unmarked)} for {pair}, but the workbook "
                        f"quotes no wings there (it has {', '.join(sorted(marks, key=pillar_years))}); "
                        f"a pillar with no mark behind it cannot be posted")
    quoted = [t for t in tenors if t != OVERNIGHT]
    if not quoted:
        raise KaceError(f"{spreads.path} lists only O/N for {pair}; at least one quoted tenor "
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
                daily=daily, pillars=pillars, notes=notes)


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
    """``xml=<url-encoded message>``, as the poster page and the desk's VBA send it."""
    return urllib.parse.urlencode({"xml": xml_text}).encode("ascii")


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
        return False, None, f"the reply is not XML: {first[:160] or '(empty)'}"
    if root.tag != "gfi_message":
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


def http_post(url: str, body: bytes, headers: dict, *, timeout: float,
              ca: str | None = None, insecure: bool = False) -> tuple[int, bytes]:
    """The real network: one POST, no proxy.  Replaced wholesale in tests.

    No proxy on purpose: the platform is on the desk's own network, and the
    corporate proxy the DTCC download goes out through is exactly the thing
    that must not see this request.  An empty ``ProxyHandler`` is what stops
    urllib installing one from the environment or the Windows registry.
    """
    handlers = [urllib.request.ProxyHandler({})]
    if url.lower().startswith("https:"):
        handlers.append(urllib.request.HTTPSHandler(context=_https_context(ca, insecure)))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(request, timeout=timeout) as reply:
            return getattr(reply, "status", 200), reply.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:8192]
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        hint = ""
        if "CERTIFICATE_VERIFY_FAILED" in reason or "certificate" in reason.lower():
            hint = (" -- the server's certificate is not one this machine trusts; name the "
                    "desk's CA bundle with --kace-ca, or --kace-insecure to post without "
                    "checking it")
        raise KacePostError(f"could not reach {url} (directly, through no proxy): "
                            f"{reason}{hint}") from None
    except (TimeoutError, OSError) as exc:
        raise KacePostError(f"could not reach {url}: {exc}") from None


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
              ca: str | None = None, insecure: bool = False, dry_run: bool = False) -> dict:
    """Post one message and write the record; the record is the return value.

    A refused post is recorded too -- with what refused it -- because the
    question the log answers is "what happened this morning", and "nothing
    reached kACE" is an answer.  A dry run records nothing and sends
    nothing: it says what *would* go, and where.
    """
    entry = {"at": when.isoformat(timespec="seconds"), "pair": pair.upper(),
             "scenario": scenario, "clear": bool(clear), "hor_date": hor_date.isoformat(),
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
                      "processing_time": result.processing_time, "reply": result.reply[:1000]})
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
