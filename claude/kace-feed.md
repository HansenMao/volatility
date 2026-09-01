# volkit §20 — The kACE feed (`kace.py`)

Extracted verbatim from `CLAUDE.md` §20. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The desk's pricing platform, kACE, takes its volatilities through an **XML
poster** page: a `gfi_message` with a `RATE_FEED` action, one `<node>` per
calendar day carrying `Maturity` and an ATM `Volity` written `bid/offer`, then
five nodes per pillar (ATM again, 25d and 10d `RR`, 25d and 10d `S`). Until
2026-09-01 that message was built in `XML_poster_DailyVol_v3.1_USDCNH_JL.xlsx`:
volkit's daily cumulative CSV pasted into one sheet, the expiry dates and the
wing row copied from Murex, a spread table typed beside them, and 3,400 rows of
formulas. Everything the sheet needed is on the book, so `kace.py` builds the
message from the book. The assessment and the round-trip against the sheet are
in `claude/kace-export-design.md`; the sheet's own output, recalculated, is
what `TestKaceFeed` pins string for string.

- **Two ways in, one function.** `volkit kace PAIR` (summary to stderr,
  message to stdout or `--out`; `--clear` for the `clearRate` message;
  `--post` to send it) and the **kACE feed** tab on the marking screen's chart
  card, on `/api/kace` (the pillar table *and* the XML in one response, so the
  copy button and the table cannot disagree), `/api/export/kace` (the
  download) and `/api/kace/post` (the button). All belong to `marking`.
- **Posting is what the poster page does, and nothing more.** The desk's VBA
  showed the wire: an HTTP `POST` to the kACE server, body `xml=<url-encoded
  message>`, `Content-Type: application/x-www-form-urlencoded`, reply a
  `gfi_message`. `post_message` does exactly that; `read_reply` takes the
  reply as success only in the one shape the poster page shows (a
  `gfi_message` whose body has a `<response>` and nothing whose tag or
  attribute says *error*), because the platform's failure vocabulary has not
  been seen and a refusal read as success is the worst outcome here.
  Everything else is a failure carrying the reply's first line. The network
  is injected (`opener`, `BookService.kace_opener`), like `dtcc.py`'s, so the
  whole path is tested against a function.
- **The page posts the request, never the XML.** `/api/kace/post` takes pair,
  cut, wings, scenario, feed-or-clear, builds the message under the book's
  lock exactly as `/api/kace` shows it, and sends that. A page cannot hand
  the platform a message this server did not make. The URL is a start-up
  setting (`--kace-url` / `VOLKIT_KACE_URL`), never the page's, for the same
  reason the proxy is not: it is where this server's credentials go.
- **No proxy, ever.** kACE is on the desk's network and the corporate proxy
  is what must not see the request; `http_post` installs an empty
  `ProxyHandler` so urllib cannot pick one up from the environment or the
  registry. `--kace-ca` names an internal CA; `--kace-insecure` skips the
  check; a certificate failure says which to use.
- **Every post is recorded**, sent or refused, in `kace_posts.jsonl` beside
  the workbook (`PostLog`; `--kace-log`): time from the book's clock, pair,
  scenario, clear, nodes, a 16-hex hash of the message, bytes, URL, outcome,
  processing time and the first kilobyte of the reply. A dry run records
  nothing. The tab shows the last ten and the confirm step is inline, not a
  browser dialog; the clear is styled as the destructive one.
- **The spread table names the pillars.** `kace_spreads.csv` beside the
  workbook (`files/` in the source tree), `pair,tenor,spread` in vol points;
  the tenors listed for a pair *are* its pillars. A tenor with no mark behind
  it is refused by name; a pair with no rows cannot be posted; a file that is
  there and wrong is refused whole and shown on the tab, like the rules file.
  `--kace-spreads` moves it. The shipped rows are the USDCNH sheet's column L.
- **The daily rule is the sheet's, spelled out.** A day takes the spread of
  the last pillar expiring on or before it; a day before the O/N expiry takes
  O/N's (`spread_for`). That was an approximate `VLOOKUP` with an `ISERROR`
  fallback.
- **Three things done differently, on purpose.** `horDate` is the book's
  valuation date, not `TODAY()`. The daily series runs to the last pillar plus
  `MARGIN_DAYS`, whatever `[daily] horizon_years` says -- the sheet's fixed
  1.0y left the 1Y expiry off the end on some weekdays and wrote the literal
  `#N/A` into the XML. And the current quoting day, whose cumulative vol is
  undefined (`cumulative_defined` false), is left out with a note rather than
  posted as zero.
- **Conventions, as the desk stated them.** RR is the base-currency call vol
  minus the put vol (kACE's *$ call* column), which is `SmileMark.rr_25` and
  `VolSurface.risk_reversal` alike. `VolType="S"` is the butterfly, and the
  `ST` mark goes there. O/N is a one-day option: `calendars.expiry_date(pair,
  "1d", today)`, posted as a pillar, borrowing the shortest quoted tenor's
  wings under `--source marks` (and saying so). `--source fitted` posts what
  the surface returns at each pillar instead -- the rrfly table's other
  column, shifts and overwrites included.
- **Expiry dates come off the calendar**, and for the sheet's 2026-01-22
  valuation reproduce Murex's nine dates to the day. Like the rest of the
  tool, "today" is the clock's UTC date.
- **The scenario is the page's, not the server's.** It is the one field of
  the message a desk changes from day to day (a test scenario before the
  live one), so the tab has a box for it and sends it as `scenario` on both
  routes; `--kace-scenario` only seeds the box (and is the command line's
  own value). The server refuses a blank one, escapes it into the attribute,
  and puts it in the download's file name. The browser remembers it in
  `localStorage`, a per-browser convenience like the ask transcript.
- **Credentials never touch the repository or the browser.** The header's
  `username` / `password` come from `--kace-user` / `--kace-password` (so
  `kace-user =` lines in `volkit.cfg`) or `VOLKIT_KACE_USER` /
  `VOLKIT_KACE_PASSWORD`. Without a username the tab still shows the pillar
  table -- it is the check on the marks -- and withholds the message, saying
  which setting is missing; the download and the command refuse outright.
- **Dates are formatted by hand** (`DD MMM YYYY`, English), because `%b`
  follows the machine's locale and the platform does not.
- Numbers are written with 15 significant figures and no exponent, which is
  what the sheet's concatenation produced; a bid that is not positive (a
  spread wider than twice the vol) is refused with the date.
