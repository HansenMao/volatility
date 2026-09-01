# volkit §17 — The quoting agent (`archive.py`, `sdr.py`, `llm.py`, `ingest.py`, `synthesis.py`, `agent.py`)

Extracted verbatim from `CLAUDE.md` §17. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The market-maker screen answers "what do I show against *this* market" and
needs a paste to fit to. A request does not arrive with a market on it, so the
agent answers "where are you on the 1 month at-the-money in a hundred million"
out of four sources: the marked surface, the knowledge bank, an archive of what
has been seen, and the same two leans (fair value, position) the market-maker
screen uses with the same caps.

Two ways in. `volkit agent <action>` on the command line, with `quote`,
`ingest`, `watch`, `evidence`, `learn`, `shown`, `outcome` and `archive`; and
a **card inside the market-maker tab** — not a seventh screen.

That is deliberate. The card answers a question *about the market on that
tab*, and a build without the market-maker screen has nothing for it to
answer, so it is three more routes on `mm` (`/api/mm/agent`,
`/api/mm/agent/ingest`, `/api/mm/agent/file`) and `agent` is one of `mm`'s
commands. `screens.SCREENS` keeps six entries, the nav and panel-map tests are
untouched, and excluding the market-maker tab takes the agent with it.

The card is `agent.SuggestPanel` and it **fits nothing**. A width comparison
needs the paste, the bank and the archive and no surface at all, so it answers
without touching the curve, the wings or the marks — which is what lets it sit
on its own button beside a fit that takes a second and a half. It posts its
own payload (`const AF=[…]` in the page) rather than the market maker's, read
by `agent.panel_from_request`, with a test pinning the two lists against each
other exactly as `MF` is pinned against `marketmaker.panel_from_request`.

Things the card decides once:

- **It reads the request box, and the paste only stands beside it.** The
  rows are the instruments asked for (`request_text`, the quote panel's own
  box); a request the paste also quoted carries that market's width beside
  it, matched on `quotes.instrument_key` the way the quote panel matches. It
  read the market paste for its rows once, and a desk read that as the agent
  answering about the wrong box -- the card sits beside the quote, and a
  width proposal about a run nobody asked to be quoted was the fit's
  question answered with the quote's tools. With the request box empty it
  falls back to the paste's rows and says so; `source` on the answer names
  which box the rows came from, and the page keys its two extra columns
  (on the quote sheet for requests, on the market sheet for the paste) and
  their freshness on that.
- **It compares widths and proposes nothing else.** Per row: what the
  market showed (blank for a request the paste did not quote), what we would
  show (the bank rule, or the panel fallback), and what the archive says
  this has actually been shown at. The verdict is
  `agrees`, `tight`, `wide`, `no rule`, `thin` or `not read`, and the quote
  sheet's width does not move until a rule is written in the bank below it.
- **Agreement is the quiet case.** A gap has to clear both a fraction of the
  archived width (`tolerance`, 10% by default) *and* an absolute floor
  (`MIN_GAP`, 0.02) before it is worth saying. Without the floor a 0.08
  butterfly "disagrees" over four thousandths and every wing row carries a
  flag forever, which is a screen nobody reads.
- **Its columns only appear beside the quotes they were computed from.** The
  page keeps the paste the last run saw (`agentFresh()`), and the two extra
  quote-sheet columns are omitted when the textarea has moved on. A stale
  width sitting next to a fresh quote is worse than no width at all.
- **The browser chooses when to scan, never where.** The watched folders come
  from `serve --chats` / `--sdr` and live on the service; the ingest route
  reads no path out of the payload, and a test pins that. A path a page can
  post is a path anything that reaches the page can read.
- **Filing the pasted run stamps it at the start of the valuation day**, not
  at the instant the button was pressed — the id is a hash of the content, so
  "now" would give a double-clicked morning a new id and count it twice in
  every width it touches. The same run under a *different* broker name is a
  genuinely new record (three brokers showing one width is stronger evidence
  than one broker three times) and is also the obvious way to double a width
  by accident, so `under_another_name` counts it and the card says so.

Things decided once, which must not be re-derived per row:

- **The output is a list of ingredients that sums to the price, and the prose
  is generated from the list.** Not the other way round. `Decision.trace` is
  the record, `Decision.facts()` renders it, `explain()` prints it and
  `llm.narrate` writes the paragraph *from it*. A story written first and
  reconciled to the numbers afterwards is a story that stays plausible when
  the numbers are wrong.
- **The archive is never quoted back at the market.** The recent market level
  is computed, shown beside the mark, and applied to nothing. A market maker
  whose mid follows the last thing it was shown is being led by the party it
  is about to trade with. A gap is a *flag*; the answer to a flag is to
  re-mark on the marking screen, deliberately.
- **The width ladder is bank, then archive, then a typed fallback, then no
  price.** Every row names the rung it stood on. A row that reaches the bottom
  shows no bid and no offer — §11's rule, unchanged, and the archive is a new
  rung on that ladder rather than a new default. The quote panel has the same
  ladder behind `QuotePanel.use_archive_width` (§18 says how it is switched
  on), and the order matters there: the bank overlay folds the typed fallback
  in when no rule matches, so the archive is tested against `spread_rule`
  and not against `spread`, or the fallback would beat it.
- **What became of our prices moves nothing.** Hit rate and adverse selection
  are the most interesting thing in the archive and the easiest to over-read:
  a run of lifted offers is sometimes a mid that is too low and sometimes a
  week of being the only one showing. `OutcomeEvidence.lean()` returns
  *words*, and the row says "shown here, and applied to nothing".
- **Thin evidence produces no number.** Weights sum to an effective count and
  below the floor (default 2.0) the answer is "not enough", named, with what
  there is. Same rule as the bank, same reason.
- **Age is a weight, not a cutoff.** `0.5 ** (age / half_life)`, default five
  days. A cutoff would make a width jump the day one observation crossed a
  line, for a reason nobody could point at. An observation with no readable
  time counts as one half-life old — treating it as current and dropping it
  are both wrong in a way that surfaces later as a width nobody can explain.
- **Nothing after the valuation time is used.** `--asof` a past date and the
  archive is read to that instant, and says how many observations it left out.
  Without this every backward-looking check on this tool flatters it.

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

Files, all beside the workbook like the bank: `mm_archive.jsonl` (the
observations), `mm_ingest.json` (what has been read). Tests are in
`tests/test_agent.py` — the one test module outside `tests/test_volkit.py`,
because most of it needs no numeric stack and runs on stdlib plus the package.
