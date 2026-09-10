# volkit §17 — The quoting agent (`archive.py`, `sdr.py`, `llm.py`, `ingest.py`, `synthesis.py`, `agent.py`)

Extracted verbatim from `CLAUDE.md` §17. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The market-maker screen answers "what do I show" out of the marked surface,
the knowledge bank and the two leans. The quoting agent is what the **archive**
adds to that answer -- an append-only file of what the market has shown, what
printed, what we showed and what became of it -- and it adds it **inside the
one pricing engine**, `marketmaker.QuotePanel` (§11). The Quote button,
`volkit mm --request` and `volkit agent quote` all arrive there, and a price
made in a browser and a price made in a shell are the same price because
there is one function that makes one.

It was not always one. This module priced on its own (`agent._decide`), with
its own request grammar (`parse_asks`) and a second copy of the width ladder
and the leans; a **Suggest** card beside the sheet compared widths on its own
(`SuggestPanel`, `/api/mm/agent`); and the quote panel had a checkbox that
put the archive on its ladder or did not. Three answers to "what do I show"
on one tab, two of them engines, and two engines that agreed on a Tuesday
disagreed by Friday. So: one engine, every row of it carrying what the agent
learned, and this module keeps what is *around* the engine -- the command
line's `Request` and `Decision`, the record (`record_quote`, `answer`) and
filing a paste. Excluding the market-maker tab from a build takes all of it.

**What the agent learns, and what each thing is allowed to do.** The scope
is deliberately what a counting agent is good at -- age-weighted statistics
with counts on them, per instrument, never a fitted model -- and each thing it
learns has one stated power:

| learns | from | does |
|---|---|---|
| **widths**, per instrument and tenor bucket, per delta | every two-way in the archive | the second rung of the width ladder, and a **verdict** on the bank's width on every row |
| **a client's record**, per instrument | the prices recorded as shown to them and the outcomes answered | **leans the mid** by their side and **widens** by what dealing with them has cost -- the one place the desk's own hit rate reaches a number |
| **the market's level**, per instrument and tenor | the same two-ways | a **flag** beside the mark, applied to nothing |
| **the printed tape**, per tenor bucket | the dissemination files | a lean, off unless `flow_weight` is set (`flow.py`); shown on the archive card and read out by the ask agent |

Things decided once on the row, which must not be re-derived anywhere else:

- **The output is a list of ingredients that sums to the price, and the prose
  is generated from the list.** Not the other way round. `row["trace"]` is
  the record, built in `QuotePanel._row` as each ingredient is read;
  `agent.Decision` is a view of the row, `Decision.facts()` renders it,
  `explain()` prints it, `llm.narrate` writes the paragraph *from it*, and the
  page shows it under every line as *how this price was made*. A story
  written first and reconciled to the numbers afterwards is a story that stays
  plausible when the numbers are wrong.
- **The width ladder is bank, then archive, then a spreading tier, then no
  price**, and it is always that ladder. Every row names the rung it stood on
  (`width_rung`). A row that reaches the bottom shows no bid and no offer --
  §11's rule, unchanged, and the archive is a rung on that ladder rather than
  a default. There is no switch for the archive: thin evidence produces no
  number, so an archive that knows nothing costs nothing, and a checkbox that
  made two prices out of one screen was the reason the engines drifted. The
  order matters: the bank overlay folds the fallback in when no rule
  matches, so the archive is tested against `spread_rule` and not `spread`,
  or the fallback would beat it.
- **The bottom rung is a ladder, not a number.** It is a **spreading tier** --
  a column of the workbook's `SPREADS` tab, the same object the kACE feed
  posts from -- named on the bar (`fallback_tier`, `--fallback-tier`), scaled
  by `fallback_multiplier`, and read at *each row's own maturity*: a tenor the
  tab names takes that rung exactly, and anything in between is stepped or,
  with `fallback_interpolate`, read straight across between the two tenors it
  falls between (`kace.width_at`). It was one typed width for every tenor on
  the screen until 2026-09-09, which is not a width any desk shows: a one-week
  and a one-year two-way are not the same width, so a single box was either
  far too wide at the front or far too tight at the back. The panel takes the
  table as `run(spreads=...)`; a tier that is not there is one message on the
  sheet (`sheet["fallback"]["error"]`) and every row the bank and the archive
  can answer is still priced. **A tenor the tab names is read off its own
  rung**, not by year fraction: the calendar's 1M is 30 or 31 days and the
  ladder's `1M` sits at 30.44, so reading by year fraction alone would show a
  quote asked for as *1M* at the 1W width.
- **The verdict is the quiet case first.** `agent_verdict` compares the width
  the bank would show (or the fallback tier) with the archive's: `agrees`,
  `tight`, `wide`, `no rule` (the archive's width is the one being shown),
  `thin`, `not read`. A gap has to clear both `tolerance` (a fraction of the
  archived width, `AGENT_TOLERANCE`) *and* `AGENT_MIN_GAP` before it is worth
  saying. Without the floor a 0.08 butterfly "disagrees" over four
  thousandths and every wing row carries a flag forever, which is a screen
  nobody reads. The verdict moves nothing: a `tight` row is still quoted at
  the bank's width, and the rule changes in the bank, by a person.
- **The archive is never quoted back at the market.** The recent market level
  is computed, shown beside the mark, flagged when they disagree by enough to
  matter, and applied to nothing. A market maker whose mid follows the last
  thing it was shown is being led by the party it is about to trade with. A
  gap is a *flag*; the answer to a flag is to re-mark on the marking card,
  deliberately.
- **A client's record moves the price, and only a client's.** The desk-wide
  hit rate is the most interesting thing in the archive and the easiest to
  over-read: a run of lifted offers is sometimes a mid that is too low and
  sometimes a week of being the only one showing, and it mixes what the desk
  was axed to do with who it was showing. `OutcomeEvidence` stays words. But
  the same counts for **one caller** on **one instrument** are the two things
  a market maker actually adjusts for, and `synthesis.ClientEvidence` carries
  them: `side` (age-weighted, +1 only ever lifts, -1 only ever hits) and
  `after_move` (how far the market went their way in the `AFTER_DAYS` after
  they dealt, read off the last quote of the same instrument and tenor in
  that window -- positive is adverse). On the row: `client_weight * side *
  half_width` leans the mid, the same shape as the axe and the same sign as
  the tape (a buyer coming is a reason to mark up), counted toward the skew
  cap with the others; and `client_weight * after_move` is added to the
  width, capped at `CLIENT_WIDEN_CAP` of the width before it. Below
  `client_min` answered prices the record is on the row and moves nothing.
  Scope is the instrument, in the tenor bucket first and across every tenor
  second, never across instruments: a buyer of the at-the-money says nothing
  about the risk reversal. A zero weight shows the record and applies none of
  it.
- **The record is kept in the book's convention.** A row asked as `JPY call
  over` carries `sign = -1`; `record_quote` turns the bid and offer back
  before filing and notes how it was asked, and `answer` swaps `traded_bid`
  and `traded_ask` (and negates `away_level`) for a row shown that way. The
  lean is applied in the book's convention and multiplied by the row's sign
  once, where the row is built. One convention in the file is what lets a
  client's record on the risk reversal be one record however each request was
  worded, and §5's first entry is what a sign kept in two places costs.
- **Recording is a button, answering is five, and both are the same functions
  the command line calls.** `/api/mm/record` files every priced row of the
  Quote answer the browser is holding (posted whole -- the server holds no
  screen state, §4) as a `shown` observation under the client on the bar,
  with the mid the model had *then*; `/api/mm/outcome` files one answer. The
  browser keeps the ids against the request lines (`MQREC`) and re-keys them
  when the pair, the request box or the client changes. A price that is shown
  and not recorded can never become evidence, and the moment to answer it is
  when the phone goes down.
- **Thin evidence produces no number.** Weights sum to an effective count and
  below the floor (default 2.0) the answer is "not enough", named, with what
  there is. Same rule as the bank, same reason; the client's minimum is the
  same rule in counts.
- **Age is a weight, not a cutoff.** `0.5 ** (age / half_life)`, default five
  days. A cutoff would make a width jump the day one observation crossed a
  line, for a reason nobody could point at. An observation with no readable
  time counts as one half-life old -- treating it as current and dropping it
  are both wrong in a way that surfaces later as a width nobody can explain.
- **Nothing after the valuation time is used.** `--asof` a past date and the
  archive is read to that instant, and says how many observations it left out.
  Without this every backward-looking check on this tool flatters it.
- **Filing the pasted run stamps it at the start of the valuation day**, not
  at the instant the button was pressed -- the id is a hash of the content, so
  "now" would give a double-clicked morning a new id and count it twice in
  every width it touches. The same run under a *different* broker name is a
  genuinely new record (three brokers showing one width is stronger evidence
  than one broker three times) and is also the obvious way to double a width
  by accident, so `under_another_name` counts it and the card says so. The
  paste is read by `agent.paste_from_request`, and a test pins the page's
  `PF` list against it, as `MQF` is pinned against
  `marketmaker.quote_panel_from_request`.
- **The browser chooses when to scan, never where.** The watched folders come
  from `serve --chats` / `--sdr` and live on the service; the ingest route
  reads no path out of the payload, and a test pins that. A path a page can
  post is a path anything that reaches the page can read.
- **A folder scan does not hold the book lock.** The archive has its own; a
  minute spent reading a large SDR file or waiting on a model is not a minute
  the pricing screen is frozen.

The model, specifically:

- **It never produces a number that reaches anything.** Extraction returns
  candidate lines that `quotes.parse_quotes` must then accept; a line the
  grammar refuses is refused, reported with its text, and never repaired.
  Narration is generated from an already-computed record.
- **The numeric guard is a set membership test, not a similarity score.**
  Every number in what the model returns must already be in what the model was
  given. `8.60` against a chat that said `8.6` passes because both canonicalise
  the same way; `8.65` does not, and the whole line goes — a line with one
  invented figure was being reasoned about rather than transcribed, and the
  rest of it is not more trustworthy for being arithmetically unremarkable.
  It is strict and it does produce false refusals; a refused line is shown so
  it can be typed by hand, which is the right trade against a fabricated level
  nobody notices for a month.
- **Everything works without it.** No model configured or none running: files
  are still ingested by the grammar, the archive still fills, the statistics
  still compute, the price is still made and the explanation is the itemised
  one. What degrades is that prose the grammar cannot read stays unread. Every
  action prints one line saying which of the two it was — a build that quietly
  used a model and one that quietly did not must never look the same.
- Transport is `urllib` and nothing else, against Ollama's `/api/chat` or any
  OpenAI-compatible `/v1/chat/completions`. No SDK: this ships as one
  executable to a desk machine that may have nothing installed. Configured
  through `VOLKIT_LLM_BACKEND` / `_URL` / `_MODEL` / `_TIMEOUT`, or the
  `--llm-*` flags.

The archive, specifically:

- **Append-only, and content-addressed.** A record is never edited and never
  deleted; a correction *supersedes* one and both stay in the file, exactly as
  `quotes.ParsedRun` keeps a superseded quote. `Archive.live()` is the view,
  `Archive.records` is the file.
- **The id is a hash of the content**, so re-reading yesterday's chat does not
  double the evidence behind a width. That is the likeliest way for this thing
  to lie — a folder rescanned nightly, every width slowly gaining confidence it
  never earned — so the fallback timestamp for a line with no clock on it is
  **the source file's** modification time and never `now`; `from_quotes`
  refuses to run without one. Levels are rounded to 6dp before hashing, because
  `8.2 * 100` is not `8.2` in binary and the same quote reached two ways hashed
  two ways.
- **Four kinds, because they are evidence of different things**: `quote` (where
  the market was shown), `trade` (where business got done, which is one side of
  somebody's market), `shown` (what we made — evidence about us, not the
  market), `outcome` (whether we were right).
- **`delta` is a fraction on the record**, 0.25 for a 25 delta — the spelling
  `quotes.py` parses into and `knowledge.Rule.delta` matches on. Points are a
  display convention and `describe()` is the one converter. Storing points
  would mean a rule lookup silently missing and the quote falling through to
  the fallback.
- Volatility is in **points** throughout the file, like the bank.

The dissemination reader, specifically:

- **Headers are matched by meaning, both layouts**, pre- and post-2022-rewrite;
  a column that cannot be placed is reported with the header that confused it.
- **A capped notional is not a notional.** Kept, flagged, and treated as a
  lower bound; never used as an equality, and `implied_from_trade` refuses to
  invert a premium against one.
- **A cancel is not a trade and a correction is not two.** Each carries the
  dissemination id it names and `Archive.resolve` ties it to the record it
  supersedes, or reports that it could not — publishers cancel prints from
  before the file existed, and a cancel silently matching nothing looks exactly
  like one that worked.
- **A premium is not a volatility.** Inverting one needs a forward, a discount
  factor and a model, all of which can be re-marked, so the economics are
  stored as published and `synthesis.implied_from_trade` does the inversion
  with its inputs named on the result.
- **The pair regex needs both legs to stand alone.** Without the boundary
  look-arounds it found `COM` + `MOD` inside `COMMODITY` and filed a crude oil
  trade under the pair COMMOD.

## Getting the trades (`dtcc.py`)

`volkit agent fetch --sdr sdr/ --since 2025-09-01` downloads DTCC's public
price dissemination files; `serve --sdr DIR` puts a **Fetch from DTCC** button
on the agent card for the last few days. What arrives is read immediately,
because a file downloaded and not ingested is a file somebody has to remember
to come back for.

Verified live from this repo: `https://pddata.dtcc.com/ppd/api/report/`
`cumulative/cftc/CFTC_CUMULATIVE_FOREX_YYYY_MM_DD.zip` answers with a zip.
DTCC keeps **366 days** and publishes nothing before **2023-12-29**.

- **Fetching and reading are two modules.** `sdr.py` must keep working on a
  desk with no route out, which is most desks this is built for, and a reader
  that could not be exercised without a network is a reader nobody can test.
- **Whatever carries the request is named on the failure, and there is a way
  past it.** `default_proxy` reads the environment; `urllib` reads more than
  that -- on Windows its default `ProxyHandler` takes the *registry's* proxy --
  so a connection could go through a proxy nothing here had named. A desk
  meeting `WinError 10061` (refused, which is not the same fault as a drop:
  that is 10060) then read "could not reach pddata.dtcc.com" while what
  refused was `127.0.0.1:8080`. `effective_proxy` is the one place that
  question is answered, for the message, for `Downloader.route` and for the
  card; `_refusal_hint` says the cure for the one failure that has a known
  one, in both directions -- a named proxy that is not listening, and a direct
  connection on a desk whose egress is a proxy urllib cannot see (it does not
  execute a PAC script). `--no-proxy` / `Downloader.direct` is the way past a
  system proxy, and it installs an empty `ProxyHandler` rather than no handler
  at all, which is what stops urllib putting its own back.
- **The network is injected** (`Downloader.opener`), like the clock is
  everywhere else. That is what lets all of `dtcc.py` be tested offline -- and
  it had better be, because the machine this is developed on has no route to
  DTCC either.
- **A 200 is not a file until it has been checked.** A proxy, a captive portal
  or an outage answers every request with HTML and status 200; trusting the
  status writes that HTML into the SDR folder, where the reader meets it
  tomorrow and reports a header it cannot place. Every body is checked for
  being a zip holding a CSV, and one that is not is refused with the first
  line of whatever came back -- usually the whole diagnosis.
- **Nothing published is not a failure.** Two days in seven have no session. A
  404 on a date DTCC keeps is reported as "nothing published"; a run that
  shouts every weekend is a run nobody reads. But "nothing published" needs
  *every* candidate URL to have said 404 -- reporting the last status instead
  called a 500 followed by a 404 on an older spelling a quiet weekend, which
  is a server falling over in disguise.
- **A date outside the window is refused before any request**, by name, rather
  than becoming a 404 the caller has to interpret.
- **The folder is the cache.** A date already on disk is not fetched again.
  One mechanism, and it is the same folder `sdr.py` reads and a person can
  open; two would be two places for it to go stale.
- **The URL is probed, not assumed.** The path has changed once. An ordered
  list of candidates is tried, the one that answered is reused for the rest of
  the run, and when none answers the error names every URL it tried -- which
  is a thing a person can paste into a browser.
- **The page chooses when, never where.** The folder and the proxy come from
  the command line; the browser sends only how many days back, capped at 30. A
  year's backfill is a command with somebody watching it, not a button that can
  be leaned on.
- One request at a time, a pause between them, a named `User-Agent`, and a
  retry with backoff on a 429 or 5xx -- a 404 is never retried, because asking
  again more slowly does not create a file. A desk that gets itself blocked
  from a public utility has broken something it cannot fix from here.

## What a printed trade teaches (`synthesis.invert_trades`)

Off by default; `agent trades PAIR --invert --history vol_history.xlsx` turns
each premium into the volatility it implies. Verified: a premium built from a
known volatility comes back as that volatility to 1e-11.

- **The forward comes from the trade's own date**, out of the historical
  workbook, interpolated across pillars by `history.forward_series`. The live
  feed is deliberately *not* a fallback for a trade that printed three weeks
  ago: inverting last month's premium against this morning's forward is wrong
  by the whole of the carry since, and wrong silently.
- **"Last row on or before" is bounded** (`MAX_STALE_DAYS`, 7). Friday's row
  is a fine forward for a Monday trade; the rule without a bound reached back
  two years for a trade the sheet did not cover, and said nothing.
- **The currency the size is in decides whether the arithmetic is well posed.**
  `premium / notional` is a price per unit of the *notional* currency; Black-76
  here wants domestic per unit of base. Notional in the base with premium in
  the quote is the straightforward case; a premium in the base is multiplied by
  the forward first; a notional on the quote leg is **refused**, because
  recovering the base amount needs the convention the trade was struck under
  and that is not published.
- **There is no discount curve, here or anywhere in this package** (§ `pricing`
  says so). Undiscounted, the volatility reads *low* by roughly the discount
  over the option's life -- about 4% of the volatility on a one-year option at
  4% rates, negligible inside a month. It is said on every row, and
  `--discount-rate` removes it.
- **Expiries are taken at midnight UTC**, because the file publishes a date and
  no cut, so a short-dated reading is a touch high. Said once per run.
- A capped notional is never inverted, and a cancelled print is not business
  that got done.
- Refusals are counted **by reason**, not listed one by one: a day of
  dissemination is thousands of rows and "1,180 had a capped notional" is the
  useful shape of that.

### The layout actually in circulation (checked against five 2026 files)

The synonym table covered both *published* layouts and still could not read a
real file, because what the file leaves **empty** matters as much as what it
carries. Verified against 330,255 rows of CFTC FOREX dissemination:

- **`Option Type` is empty in every row.** Read only from that column, no print
  in the file has a side — and `implied_from_trade` refuses a trade with no
  side, so the entire tape inverts to nothing. The side is in the **legs**:
  `Call currency` against `Put currency`. A USD call against an HKD put is a
  call on USDHKD, whatever the file calls the pair.
- **The order the pair is written in is not a convention.** The same file
  writes USDHKD as `HKD/USD` and AUDHKD as `AUD/HKD`; `Exchange rate basis`
  reads "second per first" in nine rows out of ten and the other way in the
  tenth. So nothing is read from the order: the **currencies** are matched
  against the book either way round (`_orient`), the book's spelling wins, and
  the row says so. Without this every USDHKD print was dropped as a pair the
  book does not build, which is exactly what a desk saw as an empty archive.
- **The legs say which way round the strike is written** (`_oriented`). The
  quote-currency amount over the base-currency amount is the rate to whatever
  rounding the amounts carry, and the published strike is whichever of it and
  its reciprocal that ratio is near. The ratio is a *check*, never a
  replacement: leg amounts are rounded, and a 7.75 strike against legs that
  round to 7.80 is a 7.75 strike.
- **The cap belongs to the leg the premium is divided by.** The file caps one
  leg and not the other; reading the row's flag threw away trades whose base
  leg was published in full.
- **`Execution Timestamp` beats `Event timestamp`.** On an exercise or a
  correction the event timestamp is the moment of *that*, and reading it as the
  trade dates a print days late.
- **Two thirds of a file is not options** — 27,146 option prints in 330,255
  rows. Outrights, NDFs and swaps are no longer filed as at-the-money trades;
  they are a `forward` observation carrying the rate and the date, which is
  what `synthesis.TapeForwards` builds a curve out of. On a pair the historical
  workbook has never held, that curve is the only forward there is, and it is
  what made USDHKD invertible at all.

`TapeForwards` interpolates in the **log of the rate against days** — a
constant interest differential, which is what a forward curve is — and extends
the last segment's carry no further than twice the longest date printed.
Holding the rate flat instead would price a 2028 option off a two-week
forward, and on a pegged pair the carry *is* the trade.

## The tape as market colour (`flow.py`)

The file publishes **what printed and never who bought**: no buyer, no seller,
no aggressor flag, by design. So a direction cannot be read out of it and has
to be inferred, and this module is the only place that inference lives.

- **A print above our mark was paid, one below it was given**, and the mark it
  was judged against is kept on the row so a wrong call can be argued with.
  The mark is the surface *as it stands now* and not as it stood that morning —
  the archive keeps no past curve — which is said on the read and is why the
  default half-life is a working week.
- **A print near the mark is not evidence.** Inside a tolerance it is
  `unclear`, never pushed to the side it fell on. The tolerance is half the
  archived width where the archive knows one and a fraction of the mark where
  it does not, because every market has a mid somebody disagrees with.
- **Size is vega, not notional** — a hundred million of a one-week option and
  of a two-year one are not the same amount of buying. Reported in the base
  currency per volatility point, which is the unit the quote panel's own axe is
  typed in, so the two can be compared without conversion.
- **It decides nothing.** The module produces a signed, age-weighted vega per
  tenor bucket and stops. `marketmaker.skew_for` takes it as a lean beside
  the fair value, the axe, the client and the bank, **off unless
  `flow_weight` is set**, capped with the others, and on level instruments
  only: what the tape paid for says nothing about where the skew belongs.
- **It is read in one place and shown in two.** `QuotePanel._flow` reads it
  on every quote, off the marks being quoted, with the archive card's own
  half-life and window (one evidence clock: `flow_half_life` and
  `flow_lookback_days` were a second pair of boxes and are gone; the
  tolerance stays, as `Flow tol` on the bar). The quote answer's `flow` block
  is painted on the archive card under the widths -- there was a card of its
  own, and a card that only repeated a block the quote already carried was a
  second place for the tape to be stale. `ask._answer_flow` reads the same
  `read_flow` off the same surface for a question in words (`flow` topic:
  paid, given, tape, who has been buying), so the sheet and the answer never
  disagree about which side a print was on.
- **Paid means mark up.** Customers paying means dealers are getting shorter
  and the next caller is more likely another buyer, which is the opposite sign
  to a long vega axe. A desk that would rather fade the crowd sets a negative
  weight and the same arithmetic runs backwards.

Files, all beside the workbook like the bank: `mm_archive.jsonl` (the
observations), `mm_ingest.json` (what has been read). Tests are in
`tests/test_agent.py` — the one test module outside `tests/test_volkit.py`,
because most of it needs no numeric stack and runs on stdlib plus the package.
