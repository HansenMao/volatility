"""The Excel listener: the pricing screen and the Quote button, one cell at a time.

A second, read-only port beside the web interface, for a spreadsheet to ask for
a price.  Excel's own ``WEBSERVICE()`` is the client it is shaped for: a GET, a
URL built out of cells, and one piece of text back -- so every answer here is
``text/plain``, a number or a tab-separated line of them, and nothing needs to
be installed on the desk for a formula to call it::

    =NUMBERVALUE(WEBSERVICE("http://127.0.0.1:8766/xl/price?pair=USDJPY&expiry=3m&strike=25DP&field=vol"), ".")

Three things it will not do, each for a reason:

* **Price anything itself.**  ``/xl/price`` is :meth:`BookService.price` and
  ``/xl/quote`` is :meth:`BookService.mm_quote` -- the pricing screen's strip
  and the one pricing engine (CLAUDE.md §17).  A spreadsheet and the screen
  asked the same thing get the same number, because it is the same call.
* **Move a mark or write a file.**  The web interface's own port carries
  ``/api/overwrite``, ``/api/reload``, the workbook restore and the kACE post;
  none of them is reachable from here, so this port can be opened to a desk
  network without opening those.  A quote asked for here names no client and
  is recorded nowhere -- the client lean and the record are the Quote
  button's, where somebody is looking at them.
* **Fail as ``#VALUE!``.**  ``WEBSERVICE`` turns any status but 200 into a
  bare ``#VALUE!`` and throws the reason away, which is the silent failure
  this project exists to remove.  So every answer is a 200, and one that
  could not be computed is a line starting ``#ERR:`` carrying the real
  message.  A number is always written in full precision with a ``.`` for the
  decimal point, whatever the desk's locale -- hence ``NUMBERVALUE(..., ".")``
  in the sheet, not ``VALUE``.

It binds to loopback unless told otherwise, and anywhere else only with a
token (``token=`` in the URL, since ``WEBSERVICE`` cannot send a header, or
``X-Volkit-Token``).  Every request takes the book's lock for at most
``busy_after`` seconds and answers ``#ERR: busy`` beyond that: ``WEBSERVICE``
is synchronous, and a reload holding the lock would otherwise freeze the
spreadsheet for as long as the reload takes.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import math
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import screens

DEFAULT_PORT = 8766
DEFAULT_BUSY_AFTER = 5.0
ERR = "#ERR: "

# What a pricing leg may say, as the pricing screen posts it.  A parameter
# outside this list (and outside the listener's own few) is refused by name:
# a word in the URL the model does not read is a silent default with a cell
# reference in it.
LEG_KEYS = ("pair", "expiry", "strike", "type", "side", "notional", "cut", "method",
            "spot", "forward", "points", "pip", "settle", "csa", "product", "barrier",
            "ramp", "overhedge", "buffer", "label")
QUOTE_KEYS = ("q", "tier")
OWN_KEYS = ("field", "fields", "token", "r")   # r: a refresh counter the sheet may bump


class Refused(ValueError):
    """A request answered with ``#ERR:`` rather than a number."""


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cell(value) -> str:
    """One value as Excel should read it: full precision, ``.`` decimals, no tabs."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        v = float(value)
        if not math.isfinite(v):
            raise Refused("not a finite number")
        return repr(int(value)) if isinstance(value, int) else repr(v)
    if value is None:
        raise Refused("no value")
    text = str(value)
    return " ".join(text.split()) if ("\t" in text or "\n" in text) else text


def line(row: dict, fields: list[str], what: str) -> str:
    """The requested fields of one row, tab-separated -- or the row's one error."""
    out = []
    for f in fields:
        if f not in row:
            raise Refused(f"{what} has no field '{f}'; it has: {', '.join(sorted(row))}")
        try:
            out.append(cell(row[f]))
        except Refused as exc:
            notes = "; ".join(str(w) for w in (row.get("warnings") or []))
            raise Refused(f"{what}: '{f}' {exc}" + (f" ({notes})" if notes else "")) from None
    return "\t".join(out)


def fields_of(q: dict, default: str) -> list[str]:
    if q.get("field") and q.get("fields"):
        raise Refused("give 'field' or 'fields', not both")
    raw = q.get("fields") or q.get("field") or default
    fields = [f.strip() for f in str(raw).split(",") if f.strip()]
    if not fields:
        raise Refused("no field named")
    return fields


def unknown(q: dict, allowed: tuple[str, ...]) -> None:
    extra = sorted(k for k in q if k not in allowed and k not in OWN_KEYS)
    if extra:
        raise Refused(f"not read here: {', '.join(extra)} (a leg reads {', '.join(allowed)})")


class ExcelService:
    """The listener's whole behaviour, as functions of a query and the book."""

    def __init__(self, service, token: str | None = None,
                 busy_after: float = DEFAULT_BUSY_AFTER):
        self.service = service
        self.token = token or None
        self.busy_after = float(busy_after)

    @contextmanager
    def book(self):
        lock = self.service._lock
        if not lock.acquire(timeout=self.busy_after):
            raise Refused(f"busy: the book has been held for over {self.busy_after:g}s "
                          f"(a reload or a long fit); ask again")
        try:
            yield
        finally:
            lock.release()

    def authorised(self, q: dict, header: str | None) -> None:
        if self.token is None:
            return
        given = q.get("token") or header or ""
        if not hmac.compare_digest(str(given), self.token):
            raise Refused("token missing or wrong")

    @staticmethod
    def gate(api_route: str) -> None:
        gone = screens.route_refusal(api_route)
        if gone is not None:
            raise Refused(gone)

    # -- the three answers ---------------------------------------------------

    def ping(self, q: dict) -> str:
        """``volkit`` and the valuation the prices are made at: a connectivity cell."""
        with self.book():
            if self.service.book is None:
                raise Refused(self.service.load_error or "no workbook is loaded")
            return "volkit\t" + self.service.book.clock.now.isoformat()

    def price(self, legs: list[dict], fields: list[str]) -> list[str]:
        """One line per leg, off :meth:`BookService.price`; a bad leg is its own line."""
        self.gate("/api/price")
        for i, leg in enumerate(legs):
            unknown(leg, LEG_KEYS)
            # A typed settlement box is the screen's 'typed'; a URL only
            # carries what somebody wrote into it.
            if leg.get("settle"):
                leg["settlesrc"] = "typed"
        with self.book():
            out = self.service.price({"legs": legs})
        lines = []
        for i, row in enumerate(out["legs"]):
            what = f"leg {i + 1}" if len(legs) > 1 else "the leg"
            try:
                if not row.get("ok"):
                    raise Refused(row.get("error") or "not priced")
                lines.append(line(row, fields, what))
            except Refused as exc:
                lines.append(ERR + str(exc))
        return lines

    def quote(self, q: dict, fields: list[str]) -> list[str]:
        """One line per instrument the request text names, off the one pricing engine."""
        self.gate("/api/mm/quote")
        unknown(q, QUOTE_KEYS)
        text = str(q.get("q") or "").strip()
        if not text:
            raise Refused("'q' is empty: write the request as the Quote box takes it, "
                          "e.g. q=EURUSD 1m 25d RR")
        with self.book():
            out = self.service.mm_quote({"request_text": text,
                                         "fallback_tier": q.get("tier") or ""})
        rows = out["sheet"]["rows"]
        if not rows:
            said = [str(n) for n in (out["sheet"].get("notes") or []) + (out.get("notes") or [])]
            raise Refused("nothing priced" + (": " + "; ".join(said) if said else ""))
        lines = []
        for row in rows:
            what = row.get("raw") or "the request"
            try:
                if row.get("our_mid") is None:
                    raise Refused(f"{what}: {row.get('verdict') or 'not quoted'}"
                                  + (f" -- {row['width_source']}" if row.get("width_source") else ""))
                lines.append(line(row, fields, what))
            except Refused as exc:
                lines.append(ERR + str(exc))
        return lines

    # -- the request, whole --------------------------------------------------

    def get(self, path: str, q: dict, header: str | None) -> str:
        self.authorised(q, header)
        if path == "/xl/ping":
            return self.ping(q)
        if path == "/xl/price":
            leg = {k: v for k, v in q.items() if k not in OWN_KEYS}
            return "\n".join(self.price([leg], fields_of(q, "vol")))
        if path == "/xl/quote":
            return "\n".join(self.quote(q, fields_of(q, "our_mid")))
        raise Refused(f"unknown address {path}; this port answers /xl/ping, /xl/price, /xl/quote")

    def post(self, path: str, q: dict, body: dict, header: str | None) -> str:
        """A whole range at once: ``{"legs": [...], "fields": [...]}`` -> one line a leg."""
        self.authorised(q, header)
        if path != "/xl/price":
            raise Refused(f"only /xl/price takes a POST (a range of legs), not {path}")
        legs = body.get("legs")
        if not isinstance(legs, list) or not legs:
            raise Refused("'legs' must be a non-empty list of legs")
        legs = [{k: ("" if v is None else str(v)) for k, v in (leg or {}).items()}
                for leg in legs]
        fields = body.get("fields") or q.get("fields") or q.get("field") or "vol"
        if isinstance(fields, list):
            fields = ",".join(str(f) for f in fields)
        return "\n".join(self.price(legs, fields_of({"fields": fields}, "vol")))


class ExcelHandler(BaseHTTPRequestHandler):
    excel: ExcelService | None = None

    def log_message(self, fmt, *args) -> None:  # a spreadsheet recalculating is not news
        pass

    def _text(self, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _answer(self, fn) -> None:
        try:
            text = fn()
        except Refused as exc:
            text = ERR + str(exc)
        except Exception as exc:  # noqa: BLE001 -- the real message, never a bare #VALUE!
            text = f"{ERR}{type(exc).__name__}: {exc}"
        self._text(text)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
        self._answer(lambda: self.excel.get(url.path, q, self.headers.get("X-Volkit-Token")))

    def do_POST(self) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}

        def run():
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                raise Refused(f"the body is not JSON: {exc}") from None
            if not isinstance(body, dict):
                raise Refused("the body must be a JSON object with 'legs'")
            return self.excel.post(url.path, q, body, self.headers.get("X-Volkit-Token"))
        self._answer(run)


def start(service, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
          token: str | None = None, busy_after: float = DEFAULT_BUSY_AFTER):
    """Open the listener on its own thread; returns the server (``.shutdown()`` stops it).

    A host other than loopback without a token is refused before anything
    listens: the port prices off the desk's marks, and the network is not the
    desk.
    """
    if not is_loopback(host) and not token:
        raise ValueError(f"the Excel listener on {host} needs --excel-token: anything that "
                         f"can reach that address could read the desk's prices")
    handler = type("BoundExcelHandler", (ExcelHandler,),
                   {"excel": ExcelService(service, token, busy_after)})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="volkit-excel", daemon=True).start()
    return httpd
