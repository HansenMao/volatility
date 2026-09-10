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
- **Every post is recorded**, sent or refused, in `publish_log.jsonl` beside
  the workbook (`PostLog`; `--kace-log`): time from the book's clock, pair,
  scenario, tier, clear, nodes, a 16-hex hash of the message, bytes, URL, outcome,
  processing time and the first kilobyte of the reply. A dry run records
  nothing. The tab shows the last ten and the confirm step is inline, not a
  browser dialog; the clear is styled as the destructive one.
- **The spread table names the pillars, and a tier names the widths.** The
  workbook's `SPREADS` tab is one row per tenor -- those *are* the
  pillars -- and **one column per spreading tier**, in vol points: `default`,
  plus whatever else the desk names (`wide`, `thin`, a client tier). The tier
  is chosen on the kACE feed tab from a dropdown, or with `--kace-tier`, and
  decides only how wide the ATM two-way is at each pillar; the pillars are the
  same whichever tier is posted. A cell a tier leaves blank falls back to
  `default` for that tenor, cell by cell, exactly as a pair column on `Vega
  Weights` does, so a tier that only widens the front end is two cells rather
  than a ladder. `SpreadTable` resolves that fallback once, at load, so what
  any caller reads is a complete ladder and there is no second place for it to
  be written differently. A tenor with no mark behind it is refused by name; a
  tier the tab does not hold is refused by name and told what the tab does
  hold; a table with no rows cannot be posted; a tab that is there and wrong is
  refused whole and shown on the tab, like the rules file. `--kace-spreads`
  points at a different workbook. The shipped `default` column is the USDCNH
  sheet's column L.

  **The market-maker screen reads the same tab.** The quote's bottom width
  rung is a tier off `SPREADS`, named on that screen's own bar and read at
  each quoted row's maturity (`kace.width_at`, `QuotePanel.run(spreads=...)`).
  The 2026-09-01 note argued the other direction -- that the feed should not
  take the market-maker's *learned* widths, because a re-learned quoting width
  would move a posted mark. This is not that: nothing measured flows into the
  tab, the desk still writes it, and the coupling is one ladder read by two
  screens rather than one screen's output driving the other. It is also the
  bottom rung, below the bank and the archive, so it only decides a width
  nothing else could.

  **It used to be `pair, tenor, spread`**, which tied a width to a currency: a
  desk that wanted to post one pair at two widths had nowhere to say so, and a
  new pair could not be posted until somebody typed it a whole ladder. The
  widths are a quoting policy, not a property of the currency. The old layout
  is the one shape `SpreadTable.load` names when it cannot find a header,
  because every workbook this tool has ever written has it: replace the header
  with `tenor, default` and one column per tier, one row per pillar. Tiers are
  added, edited and removed on the `SPREADS` table in the **Config**
  window like any other configuration tab -- marked into the session, written
  with the marks.
## Several pairs at once, from the market-maker bar (2026-09-09)

The feed tab posts **one pair** -- the pair the marking screen is on -- and that
is right for the card it sits on: it shows that pair's pillar table and posts
what the table shows. What a morning actually ends with is *every* pair going
out, and doing that a pair at a time means picking each one on the marking
screen and pressing Post.

So bulk publishing has a screen of its own, **Vol bulk processing**
(`claude/screen-export.md`): every channel the marks go out on, pairs from a
picker, the overlay, the export tables. The bar that used to do this on the
market-maker tab is gone and `/api/kace/bulk` with it; `publish.build("kace",
...)` builds one `Feed` per pair through `kace.build` for a pair read whole off
the book and through the pillar quotes for a pair the overlay supplied, and
`BookService.export_run` posts them one at a time, each answered on its own
line, every post in `publish_log.jsonl` as any other is.

- **Key tenors only** (`Feed.pillars_only`, `build(pillars_only=True)`,
  `volkit kace --pillars-only`) writes the pillars' five nodes each and none of
  the calendar-day ATM nodes: 45 nodes rather than 400-odd. The daily series is
  still built -- a pillar's ATM is read off it -- and simply not written, so the
  pillars posted are identical either way. It is a field of the post log, because
  "what did we send kACE this morning" is not answered by a pair and a tier when
  one morning sent the whole curve and the next sent nine points.
- **The route belongs to the market-maker screen** (`screens.py`), because the
  button is on that bar; the feed tab's own routes stay the marking screen's. A
  build without the market-maker tab keeps the feed tab and loses the bulk
  button, which is the right way round.
- **Destinations are declared by the server** (`webapp.BULK_DESTINATIONS`, on
  `/api/state`), one today. A destination the page could offer and the server has
  no route to is the silent zero this project exists to remove.

- **The post log says which tier went out.** `tier` is a field of every
  `publish_log.jsonl` entry (empty on a clear, which carries no widths) and a
  column of the tab's own history table, so "what did we send kACE this
  morning" answers with the policy as well as the pair.
- **The daily rule is the sheet's, spelled out.** A day takes the spread of
  the last pillar expiring on or before it; a day before the O/N expiry takes
  O/N's (`spread_for`). That was an approximate `VLOOKUP` with an `ISERROR`
  fallback. It stays the default because it is what was posted.
- **Two knobs on the widths, neither of which moves a mid.** The **multiplier**
  (`build(multiplier=…)`, the `×` box on the tab, `--spread-multiplier`) scales
  every one of the chosen tier's widths. A tier is a ladder somebody maintains
  on a workbook tab; a morning that wants everything half again as wide should
  not have to type a second ladder to say so, and the multiple is not a policy
  worth a column. It scales the pillars *before* the day-by-day rule reads
  them, so the screen, the XML and the log all carry the multiplied width and
  cannot disagree; blank is 1, and anything that is not a positive finite
  number is refused by name (`spread_multiplier`) rather than taken as one.
  **Interpolation** (`spread_for(..., interpolate=True)`, the *interpolate*
  checkbox, `--interpolate-spreads`) reads a day between two pillars straight
  across between their widths, by calendar date, instead of taking the last
  pillar's — so the two-way widens smoothly rather than stepping on nine
  mornings a year. It changes only the days *between* pillars: a pillar's own
  width is the tier's either way, and outside the ladder the nearest pillar's
  is still what is posted. Both are the morning's choice like the tier and the
  scenario, remembered per browser, sent on every route, and written into
  `publish_log.jsonl` (`multiplier`, `interpolate`) — a tier name alone stopped
  being the whole answer the moment it could be multiplied, so the tab's
  history column is *Widths*, not *Tier*.
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
