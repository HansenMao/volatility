# volkit §11 — Market making (`quotes.py`, `knowledge.py`, `marketmaker.py`)

Extracted verbatim from `CLAUDE.md` §11. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

A fifth UI tab. The other screens answer "what is this worth"; this one answers
"what do I show", which has three stages, kept apart because they fail for
different reasons and the screen has to say which one broke.

**None of the three moves a mark.** It did not start that way. There was a
**Fit** button on this tab that read the market paste and moved the
at-the-money curve and the four smile parameters, and a **marking agent** card
beside it that proposed the same thing more carefully and wrote the answer into
a journal. Two ways to re-mark one curve on one screen, and only one of them
learned from. So the fit moved: `marking.FitPanel` is the desk's own fit and
`marking.MarkPanel` the agent's, both on that one card, and this module keeps
the two things a desk does with a curve it is *not* changing.

## One tab, several currencies (2026-09-09)

**The pair selector has gone from the bar.** It was one currency at a time on a
screen whose two read-only stages have no reason to be: a check reads a paste
and a quote reads a request, and both are answered per line. What the selector
actually served was the *fit*, which is of one curve -- so the pair moved onto
the marking agent card, where the fit is, and everything else on the tab reads
the pair off the line it is on.

- **`CheckSheet` and `QuoteSheet` are the screen's two routes now**, and each is
  a loop over the panel above it: `pairs_named` reads the box once with no pair
  given, in order of first appearance, and one panel runs per pair with the
  other pairs' lines passed over. The rows come back **in the order they were
  written**, each carrying its `pair`, because a run is read down the page and
  not currency by currency. Everything a pair's own panel said -- its bank, its
  archive, its client record, its axe, its fair value, its tape, which marks it
  stood on -- is under `by_pair` in full; the sheet's own blocks are the sums.
  A sheet is not a second engine: a test pins a row on a three-pair sheet
  against the row that pair's panel makes alone.
- **A line that names no pair is refused**, and this is the whole reason the
  selector could go. `parse_quotes(require_pair=True)` raises `quotes.NO_PAIR`
  on it, so the line is `skipped` with the reason rather than priced. With one
  selector left on the tab -- the marking card's, which is about a *fit* -- a
  bare line has nobody to belong to, and reading it as whichever pair that card
  happens to show would be a price against a curve nobody asked for. It is
  reported **once**, not once per pair on the sheet.
- **One pair's refusal is one pair's line.** A pair the book does not build, an
  expiry in the past, a surface that will not read: caught per pair, put in
  `by_pair[pair]["error"]` and in the warnings, and the rest of the sheet is
  answered. A single panel raises and takes the screen with it, which is right
  when the screen is one pair and wrong when one mistyped pair in a broker run
  would blank a check of the other four.
- **The cut and the interpolation are the Vol marking tab's.** They were two
  boxes on this bar, which is two places to decide one thing; a cut is chosen
  on the screen where the curve is marked. `cut` travels on every request and
  `methods` maps a pair to its interpolation -- the marking tab's for the pair
  it is showing, nothing for the rest, which the server reads as each pair's own
  default. The page re-runs both stages when either moves, because a sheet run
  at last night's cut must not sit on the screen looking current.
- **Held marks reach their own pair only.** The marking card holds one pair's;
  `_marks_for` hands them to that pair's panel and every other pair reads the
  book. Laying a EURUSD fit over USDJPY's rows would be a wrong answer that
  reads perfectly well, and refusing the whole sheet because one pair on it was
  not fitted would make the card unusable beside it. Each pair's line says which
  it was, and a stale stamp is still dropped and named -- with the pair in front
  of it on the sheet's own note, because only one of several pairs went stale.
- **The cards under the sheet show every pair.** The knowledge bank is one table
  per pair with one **Save bank** over all of them (`/api/mm/bank` with `banks`,
  every pair validated before any is set); **Learn widths** learns for every pair
  on the card at once, each from the archive and its own lines of the paste; the
  archive card and the printed tape are a block per pair; **File this run** files
  every pair the paste names, under itself. *Ask the record* still takes one pair
  -- a question is about one -- and falls back to the marking card's when the
  question names none.

**Those three stages are three panels, three routes and three buttons.**
`marketmaker.CheckPanel` (`/api/mm/check`, **Check Market**) reads the market
paste and says where the marks sit against it; `marketmaker.QuotePanel`
(`/api/mm/quote`, **Quote**) reads the **request box** and makes a two-way in
each line; `marking.FitPanel` (`/api/mm/mark/fit`, **Fit my way** on the
marking card) and `marking.MarkPanel` (`/api/mm/mark`, **Propose**) are the
only things on the tab that move anything. The check puts a price on nothing,
the quote fits nothing, and neither can dirty the book.

- The two fitters themselves did **not** move. `fit_atm_curve`,
  `tune_smile_shifts`, `curve_targets`, `capture_marks`, `apply_marks` and
  `mark_fingerprint` are all still here, as model code, with `marking` as their
  only caller. What went is the *panel* that ran them from this screen. The old
  `Panel._targets` became module-level `curve_targets(surface, quotes, expiries,
  source=, text=)` for exactly that reason: leaving it hanging off the check
  panel would be a check carrying a fit's machinery around for somebody else to
  borrow.
- A check is answered against a run that has just arrived; a quote is answered
  in seconds, over and over, against whatever is marked. A request does not
  arrive with a market on it (§17 says the same thing about the quoting agent),
  so tying the two boxes together meant a request could only be priced against
  a market that had nothing to do with it.
- **They meet the marking card at `capture_marks`, and the browser carries
  it.** `FitPanel` (and `MarkPanel`, through `marks_from_snapshot`) hands back
  the parameters it arrived at -- volatility knobs in points, like every other
  number crossing this boundary -- and the check and the quote post them back
  and put them on the surface for the length of one call, under `applied_marks`,
  which restores and *verifies* the restore the way `marking.marked` does. The
  server holds no screen state (§4) and this does not change that: the marks are
  panel state and the browser owns them, in one holder (`HELD`) that also
  records which of the two produced them. A panel given no marks reads the
  surface as it stands, and both cards say which of the two it was -- a market
  checked against this morning's proposal and one checked against last night's
  marks must never read the same. Marks naming another pair are refused: the
  browser holds them and the pair selector apart and they can be moved apart.
- **Held marks are only good for the book they came from.** The other screen
  between the calls is the marking one, and the browser keeps the marks across
  a trip to it. `applied_marks` would then put the fit's backbone knobs and
  smile shifts back over whatever had been marked there -- silently, and only
  over *those two*, because they are all `capture_marks` holds. A curve
  re-marked and applied went back to the fit's; a pinned tenor, a re-quoted
  wing, a marked term structure and a band all went through. Half a screen
  agreed with itself, which is worse than none of it doing so.

  `FitPanel.run` stamps `mark_fingerprint(book, pair)` onto the marks it hands
  back: one short hash per part of `session.capture_pair`, plus the sheet's own
  quotes and wing ratios, which a session does not capture but a reloaded
  workbook moves. `CheckPanel.run` and `QuotePanel.run` recompute it, and where
  it differs the marks are **dropped** -- the panel reads the book as it stands,
  names which part moved in `marks.stale` and in a warning, and the note on the
  card says which of the three it was.

  A photograph rather than a counter, deliberately: `capture_pair` is already
  what a re-marking instance is diffed from (§18), so a marking route written
  next year is covered on the day it is written and there is no bump for it to
  forget. Marks carrying *no* stamp are read as they always were -- a payload
  from an older client is not a stale one, and refusing on a missing field would
  have broken every saved panel the day it shipped. The stamp is taken **after**
  the fit's restore-or-apply, so a fit that kept its marks is not immediately
  out of date with the book it just wrote.
- **The browser's half is one hook, in `post`.** `MARKING_ROUTES` names every
  POST route that can put a mark on the loaded book or replace it, and
  `bookMoved()` flags the held marks stale and re-runs the **check** and the
  **quote**. Never a fit: a fit moves a mark, `keep the marks` writes it, and a
  refit firing on every keystroke in the marking table would have the two
  screens marking each other. Both read-only stages then read the book, name
  what moved and say so, which is why re-running them is safe and re-running a
  fit is not. Hooked
  into `post` rather than added to each route because the routes that mark
  today are not the routes there will be -- and every marking route used to
  end with `schedulePrice()`, which is the *pricing* screen, so the
  market-maker tab showed the last run's numbers however much was re-marked.
  A test pins the list against every `BookService` method that touches
  `self.dirty` or rebuilds the book, in both directions.
- **The request box takes no prices** (`quotes.parse_requests`). One number on
  a line that has not already said what it is struck at is a strike; anything
  else is refused with the line. A broker run pasted into the wrong box would
  otherwise be quoted at levels nobody asked about.
- **A request is quoted in the convention it was asked in.** `JPY call over`
  on USDJPY carries `sign = -1` on the request, and the sign is applied
  **once**, where the row is built -- to the model value and to the bank's
  shift, before anything else reads them, so the bid is still the low side of
  what we show and no second place exists for a sign to live. §5's first entry
  is what a second place for a sign costs.
- **A request the market paste also quoted carries that market beside it**,
  matched on `quotes.instrument_key` -- what makes two lines the same quote --
  so "inside their market" survives the split. The paste is read here for that
  and nothing else: it is never fitted to on this route, and its own parse
  notes are not repeated beside a price, because the check that read it already
  reported them.
- **The fair value is measured inside the marks**, not before them. It is the
  mark against realized volatility, and the mark being shaded is the one being
  quoted; measured outside, marks that moved the at-the-money half a point
  would have their price shaded by the richness of the level they had just left.
  This moved quote numbers against the single-panel version; the client's
  record (below) is the only other thing that has since.

- **The curve.** `fit_atm_curve` puts the backbone through a target term
  structure -- the tenors pinned on the marking screen, a pasted curve, or the
  mid of the at-the-money quotes. It is a **cold** fit: the level parameters
  are read off the targets, the shape parameters are swept, and the sweep may
  only move a parameter the caller left *free* (sweeping a pinned one and then
  keeping the best node's value was a real bug -- it silently un-pinned
  `short_decay`). It runs on a `deepcopy`, so a fit the user does not keep
  cannot leave a half-marked curve behind. For a **cross** the level belongs to
  the legs, so what is fitted is the correlation term structure instead;
  `_Knobs` hides which kind of curve it is from everything above it.
- **The backbone's mean reversion is fitted inside 1.5 to 6.5, and that bound
  is a marking judgement rather than a property of the model -- so it is the
  one bound a caller may move, and it is a control on the screen rather than a
  constant in the source.** `marketmaker.MEAN_REVERSION_RANGE` is the house
  declaration, `/api/state` carries it so the panel cannot offer a range the
  fitter never heard of, and `fit_atm_curve(reversion_range=...)` takes an
  override -- the same arrangement as `relvalue.WEIGHTS`. The sweep nodes are
  a function of whichever range is in force (`reversion_nodes`), because a
  node the polish may not reach can still win the sweep on cost and is then
  clipped into the bound, which is a different curve from the one that was
  measured. Read as a half-life, `ln(2)/k`, 1.5 to 6.5 is a curve that closes
  half the gap between the front and the back end in five weeks to five and a
  half months. The ceiling is 6.5 and not 6 because AUDUSD and NZDUSD are
  marked there: a default range that excludes marks the desk has actually
  made argues with its own book on the first morning.
  It bounds **the fit and not the mark**: a value typed into the marking
  screen's parameter box is a mark somebody made on purpose and is left as
  typed. `check_reversion_range` is the one reader for the panel, the CLI
  (`volkit mm --reversion-range FLOOR CEILING`) and the fit, so a range that
  is legal on the screen cannot be illegal underneath it, and the floor is
  held above zero -- at zero the backbone is flat and the whole term structure
  is `short_addon`, which is a different model wearing the same parameters.
  **The control is hidden until it is asked for, and hiding it clears it.**
  Two empty boxes are the house range, the way an empty market box on the
  pricing screen hands the field back to the feed; one box filled is refused
  rather than half-read. A box out of sight still posting a number is the
  silent zero this screen exists to remove, so the disclosure clears both on
  the way closed and refills them from `/api/state` on the way open. The
  browser owns whether it is open, like every other piece of panel state.
  The fit says which range it ran in (`reversion_house`) and the screen states
  it **only when it was overridden**: a fit made inside the house judgement
  and one made outside it must not read the same.
  **A parameter resting on a bound is only reported when the bound is holding
  the fit back** (`_BOUND_BINDING_RMSE`): AUDUSD is marked at exactly 6.5, and
  an ungated check warned on every refit of the curve the desk already had.
  A warning that fires when nothing is wrong is one nobody reads.
- **The wings.** `tune_smile_shifts` moves the four smile parameters by an
  additive `VolSurface.param_shifts`, and is deliberately *not* a cold fit: it
  starts from the marked surface because that is the thing being adjusted. A
  **shift** rather than an overwrite, because a broker run should move the
  level of a wing and not flatten its term structure. **Curve-wide** rather
  than per tenor, because a handful of quotes does not determine a shape -- a
  shift that cannot reach a tenor says so in its residual instead of bending
  the surface to one quote.
- **The quote.** **The one pricing engine** (§17): the Quote button, `volkit
  mm --request` and `volkit agent quote` all arrive at `QuotePanel.run`. Width
  from the knowledge bank, then the archive, then a **spreading tier** off the
  workbook's `SPREADS` tab read at the row's own maturity, then no
  price -- the row names the rung -- widened by what dealing with the named
  client has cost; mid shaded by fair-value richness, by the vega already on
  the book, by the printed tape when a weight says so, and by the client's
  side, all capped together as a fraction of the width. Every row carries
  `trace`: the ordered list of ingredients, each with its value, unit and
  source, that sums to the bid and the offer. The CLI's explanation and the
  local model's paragraph are generated from that list and never the other
  way round. And every row carries the quoting agent's verdict on the width
  the bank would show (`agent_verdict`, `_agent_verdict`), which used to be a
  card of its own beside the sheet: `agrees` is the quiet case, and a gap has
  to clear both `tolerance` (a fraction of the archived width) and
  `AGENT_MIN_GAP` before it says `tight` or `wide`.

Things that are decided once and must not be re-derived per row:

- **The objective is a hinge**, not a least squares: zero anywhere inside the
  quoted bid and offer, distance to the nearer side outside it. That is the
  brief -- our mid inside the market, not on top of somebody's mid -- and it is
  what lets a dozen quotes be satisfied at once when a fit through their mids
  would satisfy none.
- **The tie-breakers are scaled to the market.** A hinge has a flat bottom, so
  a small pull toward the quoted mids picks one answer out of the many that
  work. The pull toward the marked shifts has to be multiplied by the market's
  own half width first: a shift is O(0.1) and a hinge is O(0.001), so a raw
  weight of 0.02 is not a tie-breaker, it is twenty times the violation it is
  meant to defer to. Unscaled, the fit stopped short of a market it could reach
  and reported that it had converged.
- **Neither lean applies to a risk reversal or a butterfly.** A break-even
  against realized volatility and a pasted vega position are statements about
  the *level*. Those rows carry the bank's own shift and say why there is
  nothing else.
- **Both leans point the same way.** Rich market, long position: both are
  reasons to want to sell, and you attract a seller's trade by shading the
  price *down*. Capped at a multiple of the half width so an axe can lean a
  price inside the market but never walk it out of one.
- **The bank invents nothing.** There is no built-in default width. A quote no
  rule matches gets no bid and no offer and says so; a **fallback tier**
  named on the bar is the only alternative, and the row reports which it was.
  That tier is a column of the same `SPREADS` tab the feed posts from,
  optionally multiplied and optionally read across between its tenors, so a
  width shown to a client and a width posted to the platform cannot quietly
  differ. A ladder is
  *measured* into the bank by one function, `agent.learn_widths` -- the
  archive's age-weighted widths, with the run on the screen counted unfiled
  -- behind the bank card's **Learn widths** and `volkit agent learn`; the
  paste-only `suggest_rules` / `learn_from_panel` / `volkit mm --learn` were
  a second pipeline for the same width and are gone. Proposing and saving are
  two steps.
- **A `note` rule is prose, is shown, and is never applied.** A note that reads
  like an instruction the tool silently ignores is a silent zero with better
  grammar.
- **The check.** `CheckPanel` turns each line of the paste into the one number
  the surface says at that instrument and compares it with the two-way the line
  quoted. Outside is `through`, with the gap in volatility points **and in units
  of the market's own width** -- the second is the number that says whether
  being outside matters. Inside but within `NEAR_EDGE` (a quarter of the width
  from the nearer side, a control on the bar) is `edge`, because that is one
  market move from being outside. Anything else is `in line`. A line the surface
  cannot be read at is `not checked`: a message, never a pass. A **choice price**
  has no inside and is never called near an edge -- warning about a market that
  quoted no width to be near the edge of is how a screen teaches a desk to stop
  reading its alerts. The severities are graded on the server and the screen
  colours the cell from them, so the amber band and the count in the header can
  never disagree.
- **Nothing touches the workbook, and only one route touches the book.**
  `CheckPanel.run` and `QuotePanel.run` report and then restore it exactly.
  `FitPanel.run`'s `apply` -- the marking card's *keep the marks* -- leaves the
  marks on the loaded book, in memory only, and says so; so does
  `mm_mark_record` with `apply`. Those two are the whole list.

The paste (`quotes.py`) follows §8's discipline: the volatility unit is decided
once from the whole run's level quotes and refused when they straddle 1.0; a
risk reversal's direction word is resolved against the pair, and one without a
direction word is read in the book's convention and reported once; an
unqualified `fly` inherits the panel's convention and records that it did; a
truncated offer (`8.2/6`) is refused rather than repaired.

The same parser reads a run written as **columns** -- `[time,] expiry, strike,
bid/offer` -- because a run mixes the two shapes and must not depend on which
line came first.

- **A comma is a column boundary and a price never straddles one.** That is
  the whole difference between `3M, 7.75, 8.30` (a choice at the 7.75 strike)
  and `3M 7.75 8.30` (the two-way at-the-money it has always been); with the
  commas thrown away, as `_norm` used to, they are the same line. Thousands
  separators are stripped first, so `1,000mm` is a size and not a column.
- **The strike column is the pricing screen's own vocabulary**: `ATM`, an
  absolute strike, or a delta. A bare `25d` names two strikes, one per wing, so
  it takes the call -- what `pricing.parse_strike` does with a bare `25d` --
  and says so on the row. An *absolute* strike needs no side at all: the
  volatility there is one number, `Evaluator.leg_value` never reads `is_call`
  for it, and `describe` must not call it a put.
- **A later timestamp wins.** A run is a conversation and the same tenor is
  requoted as it moves; left alone the older quote goes into the fit beside the
  newer and pulls it back. When two quotes cannot be compared on time the later
  *line* wins, which is the only ordering an untimed line carries.
  `_conflict_key` is what "the same thing" means -- a market strangle and a
  smile butterfly at one delta are deliberately two instruments.
- **A superseded quote is kept, not dropped** (`ParsedRun.superseded`), and it
  is still **width evidence**: one tenor quoted twice is one live price and two
  observations of how wide it is shown, so `archive.from_quotes` (and so
  `agent.learn_widths`) reads `all_quotes` and the fit reads `quotes`. A line read, understood and then
  silently discarded is a silent zero with better manners.
- A **column header** is recognised (no digits, two or more header words) and
  reported as passed over rather than as a line that could not be read: a
  spreadsheet paste brings one, and it is not an error.
- A date alone is an expiry; a date **followed by a time** is a timestamp.
  Reading one as the other moves a quote to a tenor nobody asked for. A
  time-only line takes the last date above it; a run with no date anywhere is
  ordered on a nominal day, says so, and never shows that day back.

Each of these is a precedence stated once and said on the row:

- **A strike beats a delta, a date beats a tenor.** `6M 25d 1.12 call` is the
  1.12 strike and `1M 30sep26 ATM` is the 30 September expiry; the other
  name is dropped with a note. Dates are read in the spellings
  `timeutil.parse_datetime` reads (`30sep26`, `30-Sep-2026`, `2026/09/30`),
  gated by shape so a number is never one.
- **A date written with the spaces in it is still one expiry.** `29 Sep`, `Sep
  29`, `29 September 2026`. `parse_datetime` reads all of these; what it never
  sees is the line, which `_consume` splits on whitespace long before it, so
  `29 Sep 6.66` arrived as the number 29 beside the word `sep` and the quote
  lost its expiry to the price. `_join_spaced_dates` glues them back before
  anything else looks at the tokens, **day first whichever way round they were
  written** -- month-first is turned round here rather than downstream, because
  `%b-%d-%Y` is not a format the date parser has and adding one there to serve
  a shape only the paste produces would put a second reading of a date into the
  package. The day is bounded to 1-31 and the year to a 19xx/20xx: a join that
  cannot be a date is worse than no join, because it turns two numbers the line
  may have meant into one token that is neither. A **bare two-digit year** is
  deliberately not taken -- `29 Sep 26` is 29 September at the 26 strike as
  readily as it is 2026, nothing in the line says which, and the line is
  refused rather than read one of the two ways.
- **A weekday name is an intra-week expiry** (`_WEEKDAYS`, `_next_weekday`):
  `fri`, `friday`, `thurs` and the rest are the next such day **strictly
  after** today -- one to seven days, never today, because a run quoting "Mon"
  on a Monday means the Monday coming and an expiry this morning is no expiry.
  It is dated in the parse, so nothing downstream knows a weekday from a
  written date, and the note says which date it landed on. Three letters or
  more only: a two-letter abbreviation is a word a sentence has in it. It
  needs the caller's `today` (as `06Nov` does) and says so when there is
  none. It is the **weakest** way to say when -- an explicit tenor or date on
  the same line beats it and it is dropped with a note (`_settle_expiries`),
  and a day name in front of a **time** is the day the run was written and
  not an expiry at all, which is what keeps `mon 09:15 1M ATM` a 1M.
- **The side matters only where it changes the number.** A volatility at an
  absolute strike is one number for the call and the put, so `_settle_side`
  drops it, `6M 1.10 call` and `6M 1.10 put` are one quote, and the later
  supersedes the earlier. With a delta the side picks the wing (`-25d` and
  `25dp` are the put). On a **premium** it is required, as is the strike --
  **except on a `live` line**, which is an option dealt without its delta
  hedge and so a low-delta one: it is out of the money, so the side is read
  from the **moneyness** against the forward, above it the call and below it
  the put. `_settle_side` leaves `is_call` open (the parse has no feed behind
  it) and says so on the row; `marketmaker.side_from_moneyness` settles it
  where the forward is known -- `premiums_as_vols` for a pasted market,
  `_premium_row` for a request -- and a side read off a strike that turns out
  to be inside 40 delta carries the warning that says to write the word.
- **A premium is a price, not a volatility** (`quote_kind`): `prem`,
  `premium`, `live`, `pips`, or a currency word right after the price make
  it one; `pips`, `%`/`pct`, `bp`/`bps` and the currency word give the unit
  (`premium_unit`: `pips`, `pct` of the base notional, or a `price` in the
  term currency per unit of base). **A basis point is a hundredth of a per
  cent**: `5bp` is carried as 0.05 `pct`, glued to its number (`5bp`,
  `12/14bp`) or as a word after it, and a number that named its own unit is
  the price, which is what makes `6M 1.25 live 12bp` a strike and a choice
  price rather than two prices. It never votes in `_decide_unit` and is
  never scaled. `marketmaker.premiums_as_vols` turns it into a volatility
  two-way **once**, against the feed's forward at its own expiry, so the fit,
  the residuals and the market table read one unit; no feed is a row that
  keeps its place with the reason. A request asked live is answered as a
  premium beside the volatility two-way it came off (`_premium_row`). The
  archive files a premium without a level. The feed's `pip` is a divisor.
- **A line naming another pair is passed over, not refused.** `USDJPY 1M
  ATM 9/9.4` under EURUSD, or a heading line that is nothing but a pair,
  goes to `ParsedRun.ignored` with the pair it named (`quotes._Blocks`),
  and the panels, the CLI and the page show it as passed over. Not in
  `skipped`: nothing on it was wrong. Without a pair every line is read and
  carries `pair` -- which is what the sheets above are built on, and why a
  line's *own* pair, not the caller's, resolves its direction word and its
  premium currency (`_build`, `_build_structure`, `_build_request`). On a
  sheet every pair is priced, so nothing was "passed over" and the note is
  dropped from the merged answer.
  **One currency names the pair** (`_shorthand_pair`, `_USD_BEHIND`): `cnh` is
  USDCNH and `eur` is EURUSD, on the line or as a heading of its own, because
  the dollar sits where the market puts it. That side is the whole content of
  the rule and is written down in one frozenset -- read it the other way and
  the pair is quoted upside down, with every sign on the line. It is resolved
  **last** in `_consume_tokens`, after every other rule has had the token: a
  currency word on a run is more often a direction (`eur call over`), a premium
  currency (`0.0125 usd`) or the currency of a size (`in 100mm eur`), each of
  which is consumed above it, and only a currency nothing else claimed becomes
  the pair. That ordering is also what makes a code that is an English word
  (TRY, PEN) no more dangerous than it was: the line has to have left it
  otherwise unread. A pair the line named in full wins over a shorthand, `usd`
  names no pair at all, and the row carries a note saying which pair a
  shorthand became -- the dollar's side is a convention, and a convention is an
  inference. The size guard matters more than it looks: read as a pair under
  another pair, `in 100mm eur` would send the whole line to `ignored` and it
  would leave the sheet silently.
  **One reading changes**, and it is the only one: a stray currency word that
  used to land in `ignored '<ccy>'` on an otherwise complete line now names
  that line's pair. Under a different pair the line is passed over rather than
  quoted -- which is the right answer for a line that says `eur` under USDJPY,
  and is worth knowing because the old reading quoted it.
  **`require_pair=True` refuses a line that names none** (`quotes.NO_PAIR`),
  which is the market-maker screen's reading and nobody else's: `volkit mm
  EURUSD` names the pair on the command line and reads a bare line as it
  always did.
- **Legs split on `vs` (or `buy`/`sell`), and what a leg does not say it
  borrows** from the legs that did (`_merge_legs`): `1M vs 3M 25d RR`, `6M
  1.10 call vs 1.15 call`. Two legs of one instrument at two tenors fold
  back into the calendar `spread` the tool always read, keyed and priced
  exactly as before (`_collapse_legs`). Anything else is a **`structure`**:
  `legs` of `QuoteLeg` with signed weights (`+`, `-`, `buy`, `sell`, `2x`),
  valued as `sum(weight * leg)` in `Evaluator.value`. Two unsigned legs are
  the second less the first and say so; three or more with no signs are
  refused -- a fly guessed one way is a fly priced upside down. A risk
  reversal leg's direction word is folded into its weight. `expiries()` is
  how every consumer walks a quote's tenors, so `resolve_expiries` sees the
  middle leg; `_row_expiry` files a row under its last leg.
