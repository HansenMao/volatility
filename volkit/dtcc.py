"""Fetching the public dissemination files from DTCC, without pretending.

``sdr.py`` reads a dissemination file. This gets one. They are deliberately
two modules: reading a file somebody put in a folder must keep working on a
desk with no route to the internet, which is most desks this is built for,
and a reader that could not be exercised without a network would be a reader
nobody could test.

What is being fetched is DTCC's **public price dissemination** -- the
anonymised trade reports the CFTC requires to be published in real time. It
is public, it is free, and it is somebody else's service, so this asks for one
file at a time, waits between requests, identifies itself by name, and backs
off when told to. A desk that gets itself blocked from a public utility has
broken something it cannot fix from here.

Four things this refuses to guess:

**Which URL.** The path has changed at least once. Rather than hard-code one
spelling and fail opaquely when it changes again, a small ordered list of
candidates is tried and *the one that answered is reported and reused* for the
rest of the run. When none answers, the error names every URL it tried, which
is a thing a person can paste into a browser.

**Whether a 200 is a file.** A service behind a proxy or a login page answers
"200 OK" with HTML all day. Every response is checked for being a real zip
holding a real CSV before it is written, and one that is not is refused with
the first line of whatever came back instead -- which is usually the whole
diagnosis.

**Whether a missing file is a problem.** There is no file for a Saturday, and
none for a holiday. A 404 on such a date is reported as "nothing published"
rather than as a failure, because a run that shouts on every weekend is a run
nobody reads.

**How far back to go.** DTCC keeps 366 days and publishes nothing before
2023-12-29. A date outside that is refused by name, before any request is
made, rather than turning into a 404 the caller has to interpret.

The network layer is injected (``Downloader.opener``), like the clock is
injected everywhere else in this package. That is what lets the whole of this
module be tested without a network -- and it had better be, because the desk
build is tested on a machine that has none.
"""

from __future__ import annotations

import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

#: The jurisdictions DTCC publishes under.  ``cftc`` is the one an FX option
#: desk wants; ``sec`` holds security-based swaps and is here so the module
#: does not have to change shape when somebody asks.
JURISDICTIONS = ("cftc", "sec")

#: Asset classes, spelled as they appear in the file names.
ASSET_CLASSES = ("FOREX", "RATES", "CREDITS", "EQUITIES", "COMMODITIES")

#: ``cumulative`` is a whole day, published after it.  ``slice`` is intraday.
REPORTS = ("cumulative", "slice")

DEFAULT_BASE = "https://pddata.dtcc.com/ppd/api/report"

#: Tried in order.  The first is the one this build has seen answer; the rest
#: are older spellings kept so a desk on a stale build still gets its data
#: when DTCC moves the path back or forward.
URL_TEMPLATES = (
    "{base}/{report}/{juris}/{JURIS}_{REPORT}_{ASSET}_{stamp}.zip",
    "{base}/{report}/{juris}/{JURIS}_{REPORT}_{ASSET}_{stamp}.csv",
)

#: Nothing is published before this.
EARLIEST = date(2023, 12, 29)

#: And nothing is kept longer than this.
RETENTION_DAYS = 366

#: Seconds between requests.  Not a rate limit anybody imposed -- a public
#: service asked for politely stays available.
PAUSE = 1.0

#: How many times a 429 or a 5xx is retried, and the first backoff.
RETRIES = 3
BACKOFF = 2.0

USER_AGENT = "volkit/1.0 (FX volatility desk tool; public dissemination reader)"


class DtccError(Exception):
    """A fetch that cannot be attempted, or a response that is not a file."""


@dataclass(frozen=True)
class Response:
    """What came back, before anything decides whether it is a file."""

    url: str
    status: int
    body: bytes
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def urllib_opener(url: str, *, timeout: float, proxy: str | None,
                  user_agent: str = USER_AGENT) -> Response:
    """The real network. Replaced wholesale in tests.

    ``proxy`` is honoured explicitly rather than left to urllib's environment
    handling, because a desk behind a corporate proxy needs the failure to say
    *which* proxy refused -- and an environment variable that is set but wrong
    is otherwise indistinguishable from no network at all.
    """
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with opener.open(request, timeout=timeout) as reply:
            return Response(url=url, status=getattr(reply, "status", 200),
                            body=reply.read(),
                            content_type=reply.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as exc:
        return Response(url=url, status=exc.code, body=exc.read()[:4096],
                        content_type=exc.headers.get("Content-Type", "") if exc.headers else "")
    except urllib.error.URLError as exc:
        where = f" through the proxy {proxy}" if proxy else ""
        raise DtccError(f"could not reach {url}{where}: {exc.reason}") from None
    except OSError as exc:
        raise DtccError(f"could not reach {url}: {exc}") from None


def default_proxy() -> str | None:
    """The proxy the environment already names, if it names one."""
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def _stamp(day: date, hour: int | None = None) -> str:
    body = f"{day.year:04d}_{day.month:02d}_{day.day:02d}"
    return body if hour is None else f"{body}_{hour:02d}"


def candidate_urls(day: date, *, base: str = DEFAULT_BASE, jurisdiction: str = "cftc",
                   asset_class: str = "FOREX", report: str = "cumulative",
                   hour: int | None = None) -> list[str]:
    """Every URL this build knows for one day's file, best first."""
    if jurisdiction not in JURISDICTIONS:
        raise DtccError(f"{jurisdiction!r} is not one of {', '.join(JURISDICTIONS)}")
    if asset_class.upper() not in ASSET_CLASSES:
        raise DtccError(f"{asset_class!r} is not one of {', '.join(ASSET_CLASSES)}")
    if report not in REPORTS:
        raise DtccError(f"{report!r} is not one of {', '.join(REPORTS)}")
    fields = {
        "base": base.rstrip("/"), "report": report, "juris": jurisdiction,
        "JURIS": jurisdiction.upper(), "REPORT": report.upper(),
        "ASSET": asset_class.upper(), "stamp": _stamp(day, hour),
    }
    return [t.format(**fields) for t in URL_TEMPLATES]


def why_not(day: date, *, today: date) -> str:
    """Why this date cannot be asked for, or an empty string.

    Checked before any request, so "that is older than DTCC keeps" arrives as
    a sentence rather than as a 404 the caller has to interpret.
    """
    if day < EARLIEST:
        return (f"DTCC publishes nothing before {EARLIEST.isoformat()}; "
                f"{day.isoformat()} is earlier than the data goes")
    if day > today:
        return f"{day.isoformat()} has not happened yet"
    if day == today:
        return (f"the cumulative file for {day.isoformat()} is published after the session "
                f"ends; ask for the day before, or fetch the intraday slices")
    if (today - day).days > RETENTION_DAYS:
        return (f"DTCC keeps {RETENTION_DAYS} days; {day.isoformat()} is "
                f"{(today - day).days} days old and has aged out")
    return ""


def looks_like_a_file(body: bytes, content_type: str) -> tuple[bool, str]:
    """Is this a zip holding a CSV, or is it a login page wearing a 200?

    The failure this catches is specific and common: a corporate proxy, a
    captive portal or a service outage answers every request with HTML and
    status 200, and a downloader that trusts the status writes that HTML into
    the SDR folder, where the reader meets it tomorrow and reports a header it
    cannot place.
    """
    if not body:
        return False, "the response was empty"
    if body[:2] != b"PK":
        head = body[:200].decode("utf-8", "replace").strip().replace("\n", " ")
        if content_type and "html" in content_type.lower():
            return False, (f"the server answered with a web page rather than a file "
                           f"({content_type}); it begins {head[:120]!r}")
        return False, f"the response is not a zip; it begins {head[:120]!r}"
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = archive.namelist()
            bad = archive.testzip()
    except zipfile.BadZipFile as exc:
        return False, f"the zip could not be opened: {exc}"
    if bad:
        return False, f"the zip is damaged at {bad}"
    if not names:
        return False, "the zip is empty"
    if not any(n.lower().endswith((".csv", ".txt")) for n in names):
        return False, f"the zip holds no CSV: {', '.join(names[:4])}"
    return True, ""


def csv_members(body: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return [n for n in archive.namelist() if n.lower().endswith((".csv", ".txt"))]


@dataclass
class DayResult:
    """One date, and what became of it."""

    day: date
    status: str = "failed"      # written | held | nothing published | refused | failed
    url: str = ""
    path: str = ""
    bytes: int = 0
    members: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)
    why: str = ""
    seconds: float = 0.0

    def line(self) -> str:
        if self.status == "written":
            return (f"{self.day.isoformat()}: {self.bytes / 1024:,.0f} kB -> "
                    f"{Path(self.path).name}")
        if self.status == "held":
            return f"{self.day.isoformat()}: already held, not fetched again"
        if self.status == "nothing published":
            return f"{self.day.isoformat()}: nothing published (a weekend or a holiday)"
        return f"{self.day.isoformat()}: {self.status} -- {self.why}"


@dataclass
class FetchResult:
    """A whole run."""

    days: list[DayResult] = field(default_factory=list)
    folder: str = ""
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def written(self) -> int:
        return sum(1 for d in self.days if d.status == "written")

    @property
    def failed(self) -> list[DayResult]:
        return [d for d in self.days if d.status in ("failed", "refused")]

    def summary(self) -> str:
        parts = [f"{self.written} file(s) written"]
        for state in ("held", "nothing published", "refused", "failed"):
            n = sum(1 for d in self.days if d.status == state)
            if n:
                parts.append(f"{n} {state}")
        return ", ".join(parts)


@dataclass
class Downloader:
    """Gets files. Knows nothing about what is in them."""

    base: str = DEFAULT_BASE
    jurisdiction: str = "cftc"
    asset_class: str = "FOREX"
    report: str = "cumulative"
    proxy: str | None = None
    timeout: float = 120.0
    pause: float = PAUSE
    retries: int = RETRIES
    user_agent: str = USER_AGENT
    #: The network, injected. Tests pass one that serves bytes from memory,
    #: which is what makes this module testable on a machine with no route
    #: out -- and the desk this is built for is often such a machine.
    opener = staticmethod(urllib_opener)
    #: The template that answered, remembered so the rest of a run does not
    #: re-probe a path that is not there.
    _working: str = ""
    sleeper = staticmethod(time.sleep)

    # ----------------------------------------------------------------------
    def get(self, day: date, *, hour: int | None = None) -> tuple[Response | None, list[str], str]:
        """One day's file: the response, the URLs tried, and why not.

        Retries a 429 or a 5xx with a growing wait, because those mean "later"
        rather than "no". A 404 is not retried: it means there is no such
        file, and asking again more slowly does not create one.
        """
        urls = candidate_urls(day, base=self.base, jurisdiction=self.jurisdiction,
                              asset_class=self.asset_class, report=self.report, hour=hour)
        if self._working:
            urls = ([u for u in urls if u.endswith(self._working)]
                    + [u for u in urls if not u.endswith(self._working)])
        tried, seen = [], []
        for url in urls:
            wait = BACKOFF
            for attempt in range(1, max(1, self.retries) + 1):
                tried.append(url)
                reply = self.opener(url, timeout=self.timeout, proxy=self.proxy,
                                    user_agent=self.user_agent)
                if reply.ok:
                    self._working = url.rsplit("_", 1)[-1]      # the extension that worked
                    return reply, tried, ""
                seen.append(reply.status)
                if reply.status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    self.sleeper(wait)
                    wait *= 2
                    continue
                break
        # "Nothing published" only when *every* candidate said 404.  Reporting
        # the last status instead called a 500 on the real URL followed by a
        # 404 on an older spelling "nothing published", which is a server
        # falling over dressed up as a quiet weekend.
        real = [c for c in seen if c != 404]
        if real:
            return None, tried, f"HTTP {real[0]}"
        return None, tried, ("HTTP 404" if seen else "no response")

    # ----------------------------------------------------------------------
    def fetch(self, days, folder, *, today: date | None = None,
              overwrite: bool = False, on_day=None) -> FetchResult:
        """Fetch each date into ``folder``, skipping what is already there.

        Already-held files are skipped rather than re-fetched: the folder is
        the cache, deliberately, because it is also exactly what ``sdr.py``
        reads and what a person can open. Two mechanisms would be two places
        for the cache to go stale.
        """
        started = time.time()
        now = today or datetime.now(timezone.utc).date()
        out = FetchResult(folder=str(folder))
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        wanted = list(days)
        if not wanted:
            out.notes.append("no dates were asked for")
            return out

        for i, day in enumerate(wanted):
            row = DayResult(day=day)
            began = time.time()
            refusal = why_not(day, today=now)
            if refusal:
                row.status, row.why = "refused", refusal
                out.days.append(row)
                if on_day:
                    on_day(row)
                continue
            name = self._filename(day)
            path = target / name
            if path.exists() and not overwrite:
                row.status, row.path, row.bytes = "held", str(path), path.stat().st_size
                out.days.append(row)
                if on_day:
                    on_day(row)
                continue
            if i and self.pause:
                self.sleeper(self.pause)
            try:
                reply, tried, why = self.get(day)
            except DtccError as exc:
                row.status, row.why = "failed", str(exc)
                out.days.append(row)
                if on_day:
                    on_day(row)
                continue
            row.tried = tried
            if reply is None:
                # A 404 on a date DTCC keeps means there was no session, which
                # is the ordinary case for two days in seven.
                if why == "HTTP 404":
                    row.status = "nothing published"
                    row.why = "no file for that date"
                else:
                    row.status = "failed"
                    row.why = (f"{why or 'no response'}; tried "
                               f"{', '.join(dict.fromkeys(tried))}")
                out.days.append(row)
                if on_day:
                    on_day(row)
                continue
            good, complaint = looks_like_a_file(reply.body, reply.content_type)
            if not good:
                row.status, row.why, row.url = "failed", complaint, reply.url
                out.days.append(row)
                if on_day:
                    on_day(row)
                continue
            path.write_bytes(reply.body)
            row.status, row.url, row.path = "written", reply.url, str(path)
            row.bytes = len(reply.body)
            row.members = csv_members(reply.body)
            out.days.append(row)
            if on_day:
                on_day(row)

        out.seconds = time.time() - started
        if not out.written and out.days:
            out.notes.append(
                "nothing new was written; every date was already held, had no session, or "
                "was refused for the reason shown against it")
        return out

    def _filename(self, day: date) -> str:
        return (f"{self.jurisdiction.upper()}_{self.report.upper()}_"
                f"{self.asset_class.upper()}_{_stamp(day)}.zip")


# --------------------------------------------------------------------------
def business_days(start: date, end: date) -> list[date]:
    """Every weekday from ``start`` to ``end`` inclusive.

    Weekdays only, because a weekend has no session and asking for one costs a
    request and produces a line of noise. Holidays are not filtered: they vary
    by jurisdiction and the fetch reports "nothing published" for them, which
    is cheaper than being wrong about somebody's calendar.
    """
    if end < start:
        start, end = end, start
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def recent_days(n: int, *, today: date | None = None) -> list[date]:
    """The last ``n`` business days that could have a file, newest last."""
    now = today or datetime.now(timezone.utc).date()
    return business_days(now - timedelta(days=int(n) * 2 + 7), now - timedelta(days=1))[-int(n):]
