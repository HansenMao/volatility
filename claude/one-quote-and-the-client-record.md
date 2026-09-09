# One quote, and the client's record

_2026-09-09. Market-maker tab (§11) and the quoting agent (§17)._

## What changed

There were three answers to "what do I show" on the market-maker tab:

- the **Quote** button (`marketmaker.QuotePanel`) -- bank width, fair-value
  and axe leans, the printed tape, and a checkbox that put the archive on the
  width ladder or did not;
- **`volkit agent quote`** (`agent._decide`) -- its own request grammar
  (`parse_asks`), its own copy of the width ladder and the leans, plus the
  trace of ingredients and the local model's paragraph;
- the quoting agent's **Suggest** card (`agent.SuggestPanel`,
  `/api/mm/agent`) -- the bank's width against the archive's, per row, and
  nothing else.

Two of those were pricing engines, and two engines that agree on a Tuesday
disagree by Friday. So:

- **One engine.** `QuotePanel.run` is the only thing that makes a two-way.
  The button, `volkit mm --request` and `volkit agent quote` all arrive at it
  through `quote_panel_from_request`. `agent.run` builds the panel from its
  `Request` and reads `Decision`s off the rows; `_decide`, `parse_asks`,
  `Ask`, `SuggestPanel`, `panel_from_request` and `/api/mm/agent` are gone.
- **Every row carries the trace** (`row["trace"]`): the ordered ingredients --
  model mid, market level (not applied), width and its rung, floor, the
  client's record, each shading, the cap, the mid, the bid and offer -- each
  with value, unit, source and detail. The CLI's explanation, the model's
  paragraph and the page's *how this price was made* are all generated from
  it.
- **The archive is always on the width ladder.** Bank, archive, typed
  fallback tier, nothing; `width_rung` names the rung. The checkbox is gone: thin
  evidence produces no number, so an archive that knows nothing costs nothing.
- **The Suggest card's verdict is a column.** `agent_verdict` on every quote
  row (`agrees` / `tight` / `wide` / `no rule` / `thin` / `not read`), with
  `agent_note`, against `tolerance` and `AGENT_MIN_GAP`. The card that is left
  is the archive itself: what it holds, and scan / fetch / file.
- **A client's record moves the price.** The one place the desk's own hit
  rate reaches a number, and it reaches it only for the caller on the phone.

## The client's record

`synthesis.ClientEvidence`, built in `synthesize` from the `shown` and
`outcome` records grouped by counterparty, per instrument, in a tenor bucket
and across every tenor:

| field | what | does |
|---|---|---|
| `side` | age-weighted, +1 only ever lifts our offer, -1 only ever hits our bid | `client_weight * side * half_width` leans the mid -- the axe's shape, the tape's sign |
| `after_move` | how far the market moved their way in `AFTER_DAYS` (5) after they dealt, off the last quote of the same instrument and tenor in that window; positive is adverse | `client_weight * after_move` is added to the width, capped at `CLIENT_WIDEN_CAP` (1.0) of the width before it |
| `answered`, `traded_bid`, `traded_ask`, `passed`, `missed`, `done_away`, `away_gap` | the counts | shown on the row and in the trace; below `client_min` (4) answered prices nothing moves |

`Synthesis.client_for(client, instrument=, days=, minimum=)` tries the tenor
bucket first and the instrument across every tenor second -- never another
instrument, never another client. Names match on spacing and case.

**The record is in the book's convention.** `record_quote` turns a
`sign = -1` row's bid and offer back before filing and notes how it was asked;
`answer` swaps `traded_bid` / `traded_ask` and negates `away_level` for a row
shown that way. The lean is applied in the book's convention and multiplied
by the row's sign once, in `_row`.

## The pieces

| Was | Is |
|---|---|
| `agent._decide`, `parse_asks`, `Ask` | `QuotePanel._row`, `quotes.parse_requests` |
| `agent.SuggestPanel`, `panel_from_request`, `/api/mm/agent`, page `AF` | `agent_verdict` on every quote row; page `PF` for filing a paste, read by `agent.paste_from_request` |
| `QuotePanel.use_archive_width`, `--archive-width`, `--no-archive-width` | always on |
| `agent.record_shown` | `agent.record_quote(archive, sheet, client=)`, `/api/mm/record`, *Record as shown* |
| `volkit agent outcome` only | `agent.answer`, `/api/mm/outcome`, five buttons per recorded row |
| `Skew(fair, axe, bank, flow)` | `Skew(..., client)`; `skew_for(client=, client_weight=)` |
| -- | `QuotePanel.client`, `client_weight`, `client_min`, `tolerance`, `include_model_read`; the bar's Client / Client wt / Client min boxes |
| -- | `volkit mm --client`, `volkit agent quote --client --client-weight --client-min --tolerance --flow-weight` |

The quote answer gained `client` (name, record, known clients, whether it was
applied), `archive.held` (every width the archive has, for the card),
`archive.records / shown / outcome / age_days`, and per row `trace`,
`width_rung`, `bank_width`, `agent_verdict`, `agent_gap`, `agent_note`,
`archive_low / high / sources / newest_days`, `client`, `client_scope`,
`client_side`, `client_after`, `client_widen`, `client_record`,
`client_enough`, `skew_client`; the sheet gained `disagreeing`,
`leaned_by_client`, `widened_by_client`, `tolerance`.

## Tests

`tests/test_agent.py` (stdlib only): `TestClientEvidence` (a buyer's side,
age weighting, the minimum, pulled is not answered, bucket-then-instrument
scope, name matching, the move against us after a lift and after a hit, done
away inside our price, a price shown to nobody is in no client's record),
`TestDecisionProse` (the facts are what the explanation may say),
`TestRecordAndAnswer` (the mid it was made from, unpriced rows refused, the
book's convention on a turned row, only the lines asked for, refusals, the two
routes), `TestPasteReader`. `TestAsks`, `TestDecision`, `TestRecordShown` and
`TestSuggestCard` are gone with what they tested.

`tests/test_marking.py`: `TestQuoteArchiveRung` rewritten for the always-on
ladder and the trace; `TestOneEngine` (the agent prices through the panel,
refuses what it refuses, the verdict on every row); `TestClientOnTheQuote` (a
buyer leans up by weight × half width, a seller down, zero weight applies
nothing, the move against us widens and is capped at one width, below the
minimum nothing moves, an unknown client, the cap holds the client's lean
with the others, a turned row turns the lean).

`tests/test_volkit.py`: the `AF` pin replaced by the `PF` pin plus the
absence of the old engine; `MQF` pinned to post the client and the agent's
settings and not the checkbox; `TestMarketMakerApi` closes the loop through
`mm_quote` / `mm_record` / `mm_outcome` and checks the next quote is leaned.

## The tape, the same day

The **printed tape** card is retired. The tape was already read by the quote
(`QuotePanel._flow`) and leaned it as the Flow column; the card only repeated
the quote's `flow` block. Now: the block is painted on the archive card under
the widths (it is one more thing the archive holds); its age weight and window
are the archive card's half-life and lookback -- `flow_half_life` and
`flow_lookback_days` are gone from the panel, the payload and the page, one
evidence clock -- and the tolerance is `Flow tol` on the bar beside `Flow wt`
and `Flow scale`. `volkit mm` and `volkit agent quote` take `--flow-weight`,
`--flow-scale`, `--flow-tolerance`. The ask agent gained the `flow` topic
(`_answer_flow`, off the same surface and the same `read_flow`) and the
`clients` topic (`_answer_clients`, off `Synthesis.clients`).

**Learn widths** was the other duplicated pipeline on the tab: the bank card's
button measured a plain median of the paste (`knowledge.suggest_rules`,
`marketmaker.learn_from_panel`, `volkit mm --learn`) while `volkit agent
learn` measured the archive, age-weighted and floored -- and the second was
not reachable from the screen. Now one function, `agent.learn_widths`: the
archive's `proposed_rules`, with the run on the screen counted **unfiled**
(stamped now for the count, checked against its filed id so a run already in
the archive is not counted twice; nothing written). `/api/mm/learn` reads the
file button's fields plus the archive card's evidence settings (page `LF`,
pinned); `volkit agent learn --file run.txt` is the same call; `volkit mm
--learn` / `--save` are gone.

Also fixed on the way: `sdr._fisn_side`'s pattern had literal backspace
characters where its `\b` word boundaries were meant to be, so it matched
nothing and no row that relied on the FISN for its side ever got one.

## The rest of the tab, examined (2026-09-09)

Every button, route and command on the market-maker tab, and whether two of
them were one pipeline wearing two coats:

| piece | pipeline | verdict |
|---|---|---|
| Check Market / `volkit mm --file` | `CheckPanel` | one; the mark against a run |
| Quote / `volkit mm --request` / `volkit agent quote` | `QuotePanel` | **consolidated** (this note) |
| Suggest card | `SuggestPanel` | **retired into the quote** |
| The printed tape card | the quote's `flow` block | **retired into the archive card**, read out by the ask agent |
| Learn widths / `volkit mm --learn` vs `volkit agent learn` | `suggest_rules` vs `proposed_rules` | **consolidated** into `agent.learn_widths` |
| Record as shown, outcome buttons / `agent shown`, `agent outcome` | `record_quote`, `answer` | one |
| File this run / Scan folders / Fetch from DTCC / `agent ingest`, `fetch` | `file_paste`, `ingest.scan`, `dtcc` | one each |
| Ask the record / `agent ask` | `ask.ask` | one; now also the tape and the clients |
| Propose / `mark propose`; Fit my way / `mark fit`; the verdict buttons / `mark record` | `MarkPanel`, `FitPanel`, `answer_from_request` | one each, by design (agent vs hand) |
| Knowledge bank editor / `agent learn --save` | `mm_save_bank`, `merge_rules` | one |
| Save marks to file | `/api/session/save` | the marking screen's own, shared |
| Evidence settings (half-life, min evidence, lookback, tolerance) | read by the quote, the marking card's archive scoring, the ask card, learn | one set, on the archive card |

Two things remain that are worth a decision rather than a change made in
passing:

1. **`volkit mark confer` had no button -- done.** Propose with an empty
   market box and *score against the archive* ticked runs the conference
   (`MarkPanel._confer`): the archive as the market, `Rounds` on the card as
   the bound, the rounds shown and the best answered with the same four
   buttons. See `claude/agent-marking.md`.
2. **The At-the-money curve and Wings cards** are the marking card's own
   result, painted from its last run. They duplicate no pipeline; folding them
   into the marking card is a layout choice, not a consolidation.

## Left open

- `after_move` reads the market's next quote of the same instrument and
  tenor. Where the archive holds no later quote (a thin pair) the widening
  never fires; the printed tape's implied volatilities could stand in, with
  the inversion's caveats named.
- A client's record is per pair. A client who is a buyer of EURUSD vol and
  asks for EURJPY is a stranger there, on purpose for now.

## The bottom rung became a ladder (2026-09-09)

The width ladder's last rung was a **fallback width**: one number typed on the
bar, used for any quote no bank rule and no archive evidence covered. That is
not a width any desk shows. A one-week two-way and a one-year two-way are not
the same width, so a single box was either far too wide at the front or far
too tight at the back, and the honest thing to do with it was leave it empty —
which is what it was mostly left as.

It is a **spreading tier** now: a column of the workbook's `KACE_SPREADS` tab,
the same ladder the kACE feed posts from, chosen on the bar beside a
multiplier and a stepped/interpolated switch, and read at **each row's own
maturity**.

- `QuotePanel.fallback_tier` / `fallback_multiplier` / `fallback_interpolate`,
  with the table handed in as `run(spreads=...)` (the browser gets it from
  `BookService.kace_spreads`, the command line loads it only when a tier is
  named). `agent.Request` carries the same three.
- `_fallback_ladder` resolves the tier **once**, before any row, into
  `sheet["fallback"]` — tier, multiplier, interpolate, the widths, the table's
  name, and an `error` when it could not be resolved. A tier that is not there
  is one message on the sheet and not a refusal: every row the bank and the
  archive can answer is still priced, which is the same rule as everywhere
  else here.
- **A tenor the tab names is read off its own rung**, exactly. Only a maturity
  the ladder does not name is read along it by year fraction
  (`kace.width_at`, stepped or interpolated). The distinction is not
  cosmetic: the calendar's 1M is 30 or 31 days and the ladder's `1M` rung sits
  at 365.2425/12 = 30.44, so reading by year fraction alone would show a quote
  asked for as *1M* at the **1W** width.
- `row["fallback_width"]` is what the tier reads at that maturity whether or
  not it was the rung used, so a bank rule can be seen beside the ladder it
  beat.
- `width_rung` is still `fallback`; what changed is what that rung *is*, and
  `width_source` now names the tier, the multiplier and which reading rule was
  in force.

**On sharing the tab with the feed.** `claude/kace-export-design.md` argued in
the other direction on 2026-09-01: the feed should not take the market-maker's
*learned* widths, because re-learning a quoting width would silently move a
posted mark. This is not that. Nothing measured flows into the tab — the desk
still writes it in the Config window — and the direction is reversed: one
ladder the desk maintains, read by two screens. It is also the *bottom* rung,
below the bank and the archive, so it only ever decides a width nothing else
could. The gain is that a width shown to a client and a width posted to the
platform cannot quietly differ.
