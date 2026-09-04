# volkit §11 — Market making (`quotes.py`, `knowledge.py`, `marketmaker.py`)

Extracted verbatim from `CLAUDE.md` §11. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

A fifth UI tab. The other screens answer "what is this worth"; this one answers
"what do I show", which has three stages, kept apart because they fail for
different reasons and the screen has to say which one broke.

**Those three stages are two panels, two routes and two buttons.**
`marketmaker.Panel` (`/api/mm/fit`) reads the market paste and does stages 1
and 2; `marketmaker.QuotePanel` (`/api/mm/quote`) reads the **request box** and
does stage 3. The fit puts a price on nothing and the quote fits nothing.

- A fit is a morning's decision, taken against a run that has just arrived; a
  quote is answered in seconds, over and over, against whatever was fitted. A
  request does not arrive with a market on it (§17 says the same thing about
  the quoting agent), so tying the two together meant a request could only be
  priced by re-running a fit against a market that had nothing to do with it,
  and a market could not be fitted without also producing prices in
  instruments nobody had asked for.
- **They meet at `capture_marks`, and the browser carries it.** The fit hands
  back the parameters it arrived at -- volatility knobs in points, like every
  other number crossing this boundary -- and the quote posts them back and
  puts them on the surface for the length of one call, under `applied_marks`,
  which restores and *verifies* the restore the way `marking.marked` does. The
  server holds no screen state (§4) and this does not change that: the marks
  are panel state and the browser owns them. A quote given no marks prices the
  surface as it stands, and `sheet.marks.note` says which of the two it was --
  a price made on this morning's fit and one made on last night's marks must
  never read the same. Marks naming another pair are refused: the browser
  holds the fit and the pair selector apart and they can be moved apart.
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
  notes are not repeated beside a price, because the fit that read it already
  reported them.
- **The fair value is measured inside the marks**, not before them. It is the
  mark against realized volatility, and the mark being shaded is the one being
  quoted; measured outside, a fit that moved the at-the-money half a point
  would have its price shaded by the richness of the level it had just left.
  This moves quote numbers against the single-panel version, and it is the
  only thing that does.

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
- **The quote.** Width from the knowledge bank, mid shaded by fair-value
  richness and by the vega already on the book, both capped as a fraction of
  the width.

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
  rule matches gets no bid and no offer and says so; a visible panel fallback
  is the only alternative, and the row reports which it was. `suggest_rules`
  proposes a ladder measured from a pasted market, with the evidence attached,
  and proposing and saving are two steps.
- **A `note` rule is prose, is shown, and is never applied.** A note that reads
  like an instruction the tool silently ignores is a silent zero with better
  grammar.
- **Nothing touches the workbook.** `Panel.run` reports and then restores the
  book exactly; `apply` leaves the marks on the loaded book, in memory only,
  and says so.

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
  observations of how wide it is shown, so `learn_from_panel` reads
  `all_quotes` and the fit reads `quotes`. A line read, understood and then
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
  carries `pair`.
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
