# Posting marks to kACE — can the XML-poster workbook fold into volkit?

Assessed 2026-09-01 against `XML_poster_DailyVol_v3.1_USDCNH_JL.xlsx`. Companion to `claude/desk-agent-design.md`. **Stages 1 and 2 were built the same day** -- see *What was built* at the end.

## Short answer

Yes, and most of it is already there. The workbook is a formatter: it takes volkit's own "save day vol" file, a spread-by-tenor table, and a row of RR/FLY marks, and lays them out as a kACE `RATE_FEED` message. Every number it needs already lives on the loaded book, and the two things the person types in by hand today — the tenor expiry dates from Murex and the RR/FLY row — volkit already computes (the calendar reproduces the workbook's Murex dates exactly for the sample valuation) or already holds (the marks sheet). The XML itself is deterministic, and a prototype built from the book's inputs reproduces the workbook's output node for node.

The one piece that cannot be settled from here is **the wire**: what the kACE XML Poster page actually sends when somebody presses *Send*. That is a five-minute capture on the desk, and it is the only thing standing between "generate the XML and paste it" and "one button". So the proposal is two stages, the first of which has no unknowns.

## What the workbook does today

Eleven steps, four of them copy-paste of thousands of rows.

1. Run volkit's daily export with *tenor* ticked — i.e. the **cumulative** (term) vol, which is `volkit daily PAIR --field cumulative`, the same series as the ↓ cumulative CSV link on the marking screen's daily card.
2. Rename it `.csv`, open in Excel, paste into `USDCNHData!A1`.
3. Copy the tenor expiry dates and the RR/FLY row from Murex into `K3:S11`.
4. The sheet builds the message: one `<node>` per calendar day carrying `Maturity` and an ATM `Volity` of `bid/offer`, where bid and offer are the cumulative vol ± half a spread; then, for each of nine tenor pillars (O/N, 1W, 2W, 1M, 2M, 3M, 6M, 9M, 1Y), five nodes — ATM bid/offer again, 25d RR, 10d RR, 25d FLY (`VolType="S"`), 10d FLY. All vols are decimals (`/100`), dates are `DD MMM YYYY`, and `horDate` is `TODAY()`.
5. Copy `XML_Consol!A1:A3398`, log in to `http://10.33.12.173:8500` as *XML Poster*, Clear, paste, Send, read the `processingTime` in the reply, then eyeball kACE's volatility page.
6. A second sheet holds the `clearRate="true"` message that wipes the pair's vols.

Three things in the sheet are worth naming because they are exactly the class of quiet failure volkit exists to remove:

- **The spread lookup is an approximate `VLOOKUP`.** A day takes the spread of the last pillar whose expiry is on or before it (so 1W's 0.8 applies until the 2W expiry), and days before the O/N expiry fall through an `ISERROR` to the O/N spread. That is a rule, and it is reproducible — but it lives in a formula nobody reads.
- **The 1Y pillar can fall off the end of the series.** The daily export runs to a horizon of 1.0 years (365.24 days); the 1Y expiry is typically 365–367 days out depending on the weekday. When it lands past the last row the pillar's `VLOOKUP` is `#N/A`, the concatenation writes the literal `#N/A` into the XML, and kACE gets a malformed value. In the sample it clears by eight days.
- **`horDate` is `TODAY()`**, so a message built in the evening and sent after midnight, or a sheet re-opened the next morning, carries the wrong horizon date silently.

## What volkit already has

| Workbook input | Where it already is in volkit |
|---|---|
| `USDCNHData!A:B` — the daily cumulative vol | `surface.atm.daily_series(horizon, cut)[date]["cumulative"]` — the same series behind `/api/export/daily` and `volkit daily` |
| `K3:K11` — tenor expiry dates from Murex | `calendars.expiry_date(pair, tenor, today)`. Checked: for a 2026-01-22 valuation it returns 29 Jan, 5 Feb, 24 Feb, 24 Mar, 23 Apr, 23 Jul, 22 Oct, 22 Jan 2027 — the workbook's dates, to the day |
| `N:O` — ATM bid/offer at each pillar | the daily series read at the pillar's expiry (that is all the sheet does) |
| `P:S` — 25/10 RR and 25/10 FLY | `book.data.marks[pair]` — `SmileMark.rr_25`, `rr_10`, `st_25`, `st_10`, in decimals; or the smile-implied values from `surface.risk_reversal` / `surface.strangle` that the rrfly table already shows |
| `L3:L11` — ATM spread by tenor | **nothing** — this is the one table volkit does not carry |
| `Currency` / `CtrCcy` | the pair name |
| `username` / `password` in the header, the URL | **nothing**, and it must not live in the repo |

The network side has a precedent: `dtcc.py`'s `urllib_opener` already handles a desk behind a proxy, names which proxy refused, and refuses to trust a 200 that is not the body it expected. kACE is on the LAN (`10.33.12.173`), so the relevant case is the *opposite* of DTCC's — the request must bypass the corporate proxy, which `effective_proxy` already understands via `proxy_bypass`.

## Verified: the XML is reproducible from volkit's inputs

A ~100-line generator was written from the sheet's layout and fed the sample's own inputs (the 373-row daily series, the nine pillars with their spreads and RR/FLY). The workbook was recalculated with LibreOffice and its `XML_Consol` column compared against the generator's output as parsed XML:

- 418 nodes in each, same names in the same order, same field sets.
- Every `Volity`, `Maturity`, `PctDelta`, `VolType`, `Currency`, `CtrCcy` value matches (numerically to 1e-12; the sheet prints 15 significant digits).
- Action options identical (`data`, `scenario="Xyz"`, `horDate`).
- The only difference is the header `timestamp`, which the sheet hard-codes to a 2006 date and the generator stamps with now.

So the formatting question is closed. The prototype is `claude/kace_proto.py` beside this note — a scratch file, not imported by anything, kept so the round-trip can be re-run.

## Proposal

### Stage 1 — generate the message (no unknowns)

A new module `volkit/kace.py`, a route and a command, both owned by the **marking** screen in `screens.py`, since the daily card is where the CSV link already sits.

```bash
volkit kace USDCNH --out usdcnh_vols.xml                 # the message, to paste
volkit kace USDCNH --clear --out usdcnh_clear.xml        # the clearRate message
volkit kace USDCNH --source fitted --out ...             # smile-implied RR/FLY instead of the quoted marks
```

On the daily card, next to ↓ cumulative CSV: **↓ kACE XML** and **↓ kACE clear**, and a *copy to clipboard* button so the paste into the poster page is the whole remaining manual step. This alone removes Excel, the rename, both pastes, the Murex date lookup, and the three quiet failures above — the person still logs in and presses Send.

Decisions inside it:

- **Horizon follows the pillars, not the config.** The daily series is run to the last pillar's expiry plus a margin, so the 1Y node can never fall off the end. If a pillar's expiry is somehow not in the series the generator refuses by name rather than emit `#N/A`.
- **`horDate` is the book's valuation date**, not the wall clock. Anything else re-creates the `TODAY()` bug.
> Superseded on the file, not on the reasoning: the spread table is the
> workbook's `KACE_SPREADS` tab now, with the holiday and band settings
> that moved with it (`volkit/configsheets.py`). Everything below about
> *what* the table holds and why still stands.

- **The spread table gets a file**, `files/kace_spreads.csv` with `pair,tenor,spread` rows, loaded like `holiday_overrides.csv`, and a missing pair is an error not a default. The sheet's column L becomes its first nine rows. (It could instead share the market-maker's learned width ladder, but that is a quote width, not the width the desk wants to show a pricing platform, and tying them would move a posted mark when somebody re-learns quoting widths.)
- **The daily lookup rule is kept as the sheet has it** — last pillar on or before the day, first pillar before any expiry — and stated in the module docstring, since it is the one piece of the sheet that was a rule in disguise.
- **RR/FLY default to the quoted marks**, which is what the sheet posts (it copies them from Murex; volkit's marks sheet is the desk's copy of the same numbers). `--source fitted` posts what the surface actually returns at the pillar, which is the rrfly table's other column.
- **One message per pair.** A clear and a feed for two pairs are two messages; kACE's `clearRate` is per node and a half-applied multi-pair message is worse than two round trips.
- Credentials for the header (`feeuser`/…) come from a `[kace]` section that is **not** `volkit.toml` in the repo: `files/kace.cfg` in `.gitignore`, or `VOLKIT_KACE_USER`/`_PASSWORD` in the environment, with the URL and scenario beside them.

### Stage 2 — post it (one capture on the desk)

`volkit kace USDCNH --post` and a **Post to kACE** button. What is needed first: on the desk, with the browser's developer tools open on the Network tab, press *Send* once, and note the request URL, method, headers (in particular whatever the login put in — a cookie, a bearer token, or nothing because the XML header's own username/password is the authentication) and whether the body is the raw XML or wrapped in JSON. The `#!/login` in the poster URL says an AngularJS front end, which is usually a thin shell over an HTTP endpoint that takes the XML as-is; the `feeuser` credentials inside the message suggest kACE's feed gateway authenticates the message itself, in which case the page login may not matter at all. Either way, the answer is on the wire and nowhere else.

Then, following `dtcc.py`:

- the reply is parsed, `processingTime` is shown on the card, and a reply that is not a `gfi_message` (a login page, a proxy's HTML, a 500) is shown as the first line of what came back;
- `--no-proxy` / `proxy_bypass` for a LAN address, named in the error if the connection is refused;
- **nothing is posted without a dry run available**: `--post --dry-run` prints the message and the URL and stops;
- every post appends a line to a `kace_posts.jsonl` beside the session file — pair, horDate, node count, a hash of the message, the reply's processing time — so "what did we send kACE this morning" has an answer that is not somebody's memory.

### Stage 3 — read it back (optional)

If kACE's XML interface has a query function (the poster page's *Query Results* box suggests it does), a read-back after posting would let the card show kACE's ATM at three pillars against what was sent, which replaces step 10 (eyeballing the volatility page). Not needed for the workflow; worth asking the kACE documentation about.

## Things to confirm before Stage 1 is marked

- **Risk-reversal sign.** volkit's `rr_25` is *call minus put* with the call on the base currency; the sheet's Murex row for USDCNH shows −0.25 and the kACE page labels its column *risk reversal ($ call)*, so both sides look like USD-call-over. The marks sheet currently holds +0.385 for USDCNH 1W against Murex's −0.25 from January — a market move, not a sign flip, but check one live day side by side before the first post.
- **Strangle versus butterfly.** The sheet posts Murex's *FLY* as `VolType="S"`; volkit's `st_25` is the *market strangle*. On the kACE page the same column is headed *butterfly*. These are the same number under two names on most desks, but if kACE's `S` is the smile (two-vol) strangle and the desk marks market strangles, the wings will be posted a few basis points wrong at every tenor. One comparison against the kACE page settles it.
- **The O/N pillar.** The sheet has one (spread 1.0, its own RR/FLY); volkit's tenor grid starts at 1W. Options: derive an O/N pillar from the 1W marks, post RR/FLY from 1W onward and let kACE carry the wings flat inside a week, or add O/N to the marks sheet. The first is what the sheet effectively does when the Murex row is copied; the second is the honest one if nobody actually marks an O/N smile.
- **Pairs.** The workbook is USDCNH only. The generator is pair-generic; the spread table decides which pairs can be posted.

## Effort

Stage 1 is a morning: the generator exists and is verified, the rest is a route, a command, a CSV loader, the two links and the tests (round-trip against the sample, the horizon guard, the spread rule, a missing spread refused, `horDate` from the clock not the wall). Stage 2 is an hour once the capture is in hand, plus tests against an in-memory server the way `dtcc.py`'s are.

## What was built (stage 1, 2026-09-01)

`volkit/kace.py`, wired into the marking screen and the command line; `CLAUDE.md` §20 is the standing description.

```bash
volkit kace USDCNH --kace-user feeuser --kace-password … --out usdcnh_kace.xml   # the feed
volkit kace USDCNH --clear --out clear.xml                                       # the clearRate message
volkit kace USDCNH --source fitted                                               # wings off the surface
volkit serve --kace-user feeuser …          # or kace-user = … in volkit.cfg, or VOLKIT_KACE_USER
```

On the page: a **kACE feed** tab beside *Daily vols* on the Vol marking chart card -- the pillar table (expiry, ATM bid/offer, RR and butterfly, where the wings came from), the notes, a *wings* switch between the quoted marks and the fitted surface, a **scenario** box (the kACE scenario the message posts into -- typed on the tab, remembered per browser, seeded from `--kace-scenario`, never blank), **Copy XML**, **↓ feed XML**, **↓ clear XML**. Routes `/api/kace` (table and message in one response) and `/api/export/kace`; both belong to `marking`, as does the command.

Decisions taken while building, where they differ from the proposal above:

- **The spread table names the pillars.** Rather than a horizon setting deciding which tenors are posted, the rows in `files/kace_spreads.csv` (`pair,tenor,spread`) *are* the pillars, and the daily series runs to the last of them plus three days. A tenor listed with no mark behind it is refused by name; a pair with no rows cannot be posted. The shipped rows are the sheet's column L for USDCNH; other pairs get rows when the desk wants them posted.
- **Conventions, as stated by the desk:** RR is the base-currency call vol minus the put vol (`SmileMark.rr_25`, `VolSurface.risk_reversal`); `VolType="S"` is the butterfly and the `ST` mark goes there; O/N is a one-day option, expiring on the next business day off the calendar, posted as a pillar, borrowing the shortest quoted tenor's wings under `--source marks` (the note on the tab says so).
- **Without a username the tab still shows the table** -- it is the check on the marks -- and withholds the message, naming the setting. The download and the command refuse outright.
- **The current quoting day is left out**, with a note, when its cumulative vol is undefined (valued before the cut); the sheet had no such row because the export was taken after the cut.
- Dates are formatted by hand so `%b` cannot follow a non-English locale; numbers carry the sheet's 15 significant figures with no exponent; a bid that is not positive is refused with its date.

Verified: `TestKaceFeed` (14 tests) pins the sheet's own recalculated strings -- node 1, the 1W and 2W expiry days, the last row, every wing node at O/N and 1Y -- against a `Feed` built from the same inputs; the spread rule; the horizon reaching the 1Y expiry and `horDate` being the book's date on the shipped workbook; the O/N pillar; the fitted wings landing near the marks; the refusals; the web service and both downloads; route and command ownership. The rest of the suite passes with it (839 tests; three pre-existing failures on the Mac VM's Python 3.10 -- `fromisoformat` and `tomllib` are 3.11 -- fail identically on the untouched HEAD).

## Stage 2, built (2026-09-01, later the same day)

The capture was not needed: the desk's own VBA for the pricer showed the wire, and the poster page is the same thing. An HTTP `POST` to the kACE server, body `xml=<url-encoded message>`, `Content-Type: application/x-www-form-urlencoded`, reply the platform's `gfi_message` (the one with `processingTime` in the header and a `<response>` for the action).

```bash
volkit kace USDCNH --post --kace-url https://pfcshkwapp01:8500/pricing   # send, record, exit 1 if not taken
volkit kace USDCNH --post --dry-run                                  # what would go, and where
volkit serve --kace-url https://pfcshkwapp01:8500/pricing --kace-ca desk.pem   # the buttons on the tab
```

- **Post feed** / **Post clear** on the tab, shown only when a URL is set; one inline confirmation naming the pair, node count, scenario and address (the clear styled as the destructive one); the outcome under it, in red with the first line of the reply when it is not the one shape that means success. The last ten posts are listed under the pillar table from the log.
- **The page posts the request, never the XML** (`/api/kace/post`): the server rebuilds the message under the book's lock exactly as the tab showed it and sends that. The URL is a start-up setting, never typed on the page -- it is where the credentials go.
- **`read_reply` is deliberately narrow**: a `gfi_message` with a `<response>` and nothing whose tag or attribute says *error* is success; everything else is a failure with the reply's first line. The platform's failure vocabulary has not been seen; a refusal read as success is the worst outcome here, so the narrow reading is the safe one and the first real error will say what to widen.
- **No proxy, ever**, on the way to kACE; an internal `https` certificate is named with `--kace-ca` or skipped with `--kace-insecure`, and a certificate failure says which.
- **`kace_posts.jsonl`** beside the workbook: every post, sent or refused, with the book's time, pair, scenario, feed/clear, nodes, a hash of the message, bytes, URL and outcome. A dry run writes nothing.
- The network is injected, like `dtcc.py`'s: six more tests (`read_reply` on the poster page's own reply and on four kinds of not-success, the form body, `post_message` through a fake, the log and a refused post, the route sending exactly the table's message and the clear, the options). 845 in the suite. A live run against a stub kACE on the Mac received 413 nodes form-encoded and was logged.

Still open: one live day of RR and butterfly compared side by side with the kACE page before the first real post into the live scenario, since the sign and the strangle-versus-butterfly reading were confirmed in words and not yet against the platform -- post into a test scenario first, which is what the scenario box is for. The address is confirmed by the desk as `https://pfcshkwapp01:8500/pricing`, and the shipped `files/volkit.cfg` now carries it.
