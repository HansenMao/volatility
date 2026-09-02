# volkit §4 — Invariants — breaking these is a regression

Extracted verbatim from `CLAUDE.md` §4. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

- **The clock is injected.** Nothing calls `datetime.utcnow()` inside the model.
  One `Clock` per book; same clock ⇒ identical numbers.
- **One year length: 365.2425.** The legacy had six.
- **Daily variances sum to the term variance** (holds to 2e-16). Any change to
  the day grid must preserve this.
- **Integration splits at known breakpoints** (hourly edges, event times), then
  fixed Gauss-Legendre. Order 5 ⇒ order 20 changes nothing. Never reach for
  adaptive `quad` on this integrand.
- **Every solve is bracketed** and raises `ConvergenceError` with a diagnosis.
  No bare `fsolve`, no fixed-iteration loops.
- **Smile slices are cached** per (expiry, method, cut, forward). The legacy
  re-ran a 12-parameter optimisation per strike query.
- **Use `scipy.special` ufuncs** (`ndtr`, `ndtri`, `log_ndtr`), not
  `scipy.stats.norm`, in inner loops. That alone was a 13x calibration speedup.
- **A smile greek is differenced on the forward.** `VolSurface.smile_delta`
  and `smile_gamma` bump the level Black-76 is priced off and the level the
  smile's own strike ratio is taken against, and that is the *forward*: the
  first argument is named `fwd` for that reason. It was named `spot`, and the
  pricing screen took the name at its word and handed it spot, which on any
  pair with forward points asks about a different option from the one the row
  above was priced on -- a 3M EURUSD ATM came back a 44.6 delta against a
  Black 50.0, and a USDJPY ATM a 29.3 against 50.0. Every other caller
  (`analytics.carry_table`, the relative-value tables) was already passing a
  forward, so nothing but that one row was wrong and nothing but that one row
  moved. The tests that pin the smile delta against `black.delta + vega *
  dsigma/dF` pass a forward too, which is why they never saw it.

- **The smile chart's strike axis is a scale, not a model change.** When the
  feed covers the pair, `/api/smile` carries `spot` / `forward` from the one
  lookup the band model uses (`Book.market_level`) and the page multiplies the
  axis, the point table and the density by it; without a feed it stays in K/F
  and says so. The slice itself is always built in moneyness. `volkit smile`
  prints the same two ways, off the same call.
- **A forward is a price for a settlement date, and that is where it is
  read.** `Book.market_level_for(pair, expiry)` is what a screen calls: it
  puts the option's own settlement date -- the spot lag past its expiry, on
  the pair's calendar -- onto the feed's axis, which is years from the spot
  date. `Book.market_level(pair, t)` reads that same curve at a *time* and
  survives only for the callers that genuinely have one and not an expiry.
  A caller holding **two** forwards to difference -- a carry row, which is the
  option's forward now against the same option's a horizon later -- puts the
  settlement date on the axis once (`Book.settlement_years`) and takes the
  horizon off it, because reading one leg on a date and the other at a year
  fraction contaminates the difference with the gap between the conventions:
  a third of a pip, the same order as the gamma carry being measured. Reading an
  option's forward at its expiry drops the spot lag, which on a one-week
  option is a fifth of its swap points; it is also what stopped a quoted
  pillar reading back as the swap points the broker actually published, since
  the query and the pillar were then two different points on one curve. It is
  an **offset** and not an absolute date, deliberately: a feed file carries
  its own spot date, and one written last Tuesday and priced today has one a
  few days behind the book's -- read absolutely, a stale file's 3M is asked
  for three months past its own three-month pillar, and a file a year out
  falls off the end of the curve and comes back at spot. Read as an offset it
  asks for "the three-month point", which is what a pillar is, and the
  staleness stays a note on the feed instead of a forward silently held flat.
  A caller may **state** the settlement date -- `market_level_for(pair,
  expiry, settle)`, and `pricing.leg_dates(book, pair, text, settle)` for the
  whole date bundle -- because a broken date is something two counterparties
  agree and not something a holiday table knows, and the pricing screen's
  Settlement box is where a desk says so. What is stated is the **date**,
  never the placement: the level still goes through `settlement_years`, still
  as an offset from the book's own spot date, so there is still exactly one
  way a forward is put on the feed's axis. A stated date moves the forward
  and nothing else -- the expiry is what the option is worth time on and does
  not move with it -- a date before the expiry is refused, and a date the
  calendar would not settle on is taken and reported
  (`pricing.settlement_note`), because refusing it would refuse the one case
  the box exists for.
- **One place reads a level, and a cross it does not hold it builds.**
  `Book.market_level` is that place: spot, the outright forward, the points
  and the pip, for the band model, the strike axis, the carry table, the
  relative-value grid, a pricing leg with a blank spot and the market-maker
  sheet's absolute strikes -- all of them through `market_level_for` above,
  which is the settlement date's spelling of the same lookup. When the feed does not quote the pair itself but
  quotes both of its legs, the level is **composed from them** -- the two spot
  rates and the two swap points, which is all an implied cross rate has ever
  been. EURJPY is EURUSD x USDJPY, EURGBP is EURUSD / GBPUSD, by
  `cross.infer_leg_signs` read as quotation rather than as correlation. The
  arithmetic is `feed.compose_level` and there is exactly **one** of it:
  `MarketFeed.level` / `quote` / `quote_on` compose, and `Book.market_level`
  calls that same function and adds only the workbook's opinion about which
  legs a cross has. A sheet that names them still wins; a cross nobody named
  takes `cross.dollar_legs`, so a pair no spreadsheet mentions -- GBPJPY off a
  file holding GBPUSD and USDJPY -- is priced rather than refused, and
  anything holding a feed and no book (the feed status route's own quote box)
  stops reading `no feed for 'EURJPY'` off a file that quotes both its legs. A
  second copy of that arithmetic would be a second place for the triangle's
  signs to be written upside down, which is §5's first entry. `quote_on` reads
  each leg on its **own** spot date, so a cross of a T+1 pair and a T+2 pair
  is placed exactly rather than at one shared `t` -- the tom-next is a day
  wide, and a day is the whole of it. That is triangular arbitrage and
  not a model, and it is why a loaded feed is no longer invisible to a cross:
  every screen used to ask the feed for the pair *by name*, so the
  market-maker screen refused a strike quote on EURJPY while the pricing
  screen quoted both its legs off the same file. `derived` and `via` travel
  with the level and every screen shows them, because a level that came out
  of an identity and one that was published must not read the same. Half a
  triangle is still a refusal -- no NZDUSD in the file, no GBPNZD forward --
  and the points are the cross's own in the cross's own pips, never the legs'
  points added.
- **A tenor is a settlement date, and the expiry comes back from it.**
  `calendars.fx_dates` is the one construction and it returns all four dates
  together -- trade, spot, expiry, delivery -- because a screen that shows one
  and computes another from a year fraction is a screen with two answers to
  one question. The order is the market's: spot date first (the pair's own
  calendars for the count, then rolled forward to a day USD can settle on --
  US holidays rule out a value date, they do not stop the count, or EURJPY
  spot would move every Thanksgiving); then the tenor added to it and adjusted
  modified following, with the **end-of-month rule** (off a spot that is its
  month's last value date, a month tenor settles on the last value date of its
  month, so a 1M off a 28-Feb spot settles 31-Mar and not 28-Mar); then the
  expiry, the spot lag back on the pair's own calendars. **Day tenors are the
  other way round**, because that is what they mean: `O/N` expires on the next
  business day and settles from that day's own spot, `8D` on the eighth.
  Adding calendar days to the spot date and taking the lag off the end -- what
  this used to do -- collapses them, because the two days subtracted swallow
  the weekend the addition just crossed: dealt on a Wednesday, `1D` and `2D`
  both came back Thursday.
- **A quoted tenor sits on the volatility axis where its calendar expiry is.**
  `calendars.expiry_years` is that reading, reached through
  `AtmCurve.tenor_years`, `VolSurface.tenor_years` and `Book.tenor_years`. A
  `1M` is not 0.083333 years; it is the years to the 1M expiry date, which is
  30 or 31 days and not a nominal 30.44. Marks are calibrated there, the ATM
  overwrites are anchored there, and the analysis, curve, monitor, band and
  market-maker tabs all read there -- because a `1M` mark that is not the
  volatility a `1M` option receives is a mark on nothing. `timeutil
  .tenor_to_years` survives as what it always was: a nominal length and a sort
  key, needing no pair and no clock, and it is still the right thing for
  ordering a list of tenors or sizing a history window. It is **not** the
  right thing for placing an option. See MIGRATION.md 1.6 for what moved.
- **A feed pillar is named by a tenor or by a date, and both land on one
  axis: years from the spot date.** A tenor sits at its **own delivery date's**
  days from spot over 365.2425, a date at its days from spot over the same,
  and they are the same number for the same pillar -- so a dated file loaded
  beside a tenor one has no seam in it. The tenor half of that used to be
  `tenor_to_years`, a nominal 30.44-day month for a month that is 30 or 31,
  which put a pillar up to a day and a half from the date it is quoted to: a
  broker's 1M swap points are the points to the 1M *value date*, and an option
  read at its own settlement date then interpolated between two pillars
  instead of landing on one. It is the one thing that was not actually on the
  axis this paragraph names. Without a valuation date there is no delivery
  date to compute and the nominal length is all there is, which is what a feed
  loaded with no clock falls back to. What a date buys is the front of the
  curve, which is not on standard tenors at all: the overnight and the
  tom-next are each quoted as **one day** of points rather than as points from
  spot, because points from spot are zero at spot and there is nowhere else to
  put them. So a dated row is read by where it lands -- after the spot date it
  is points from spot, on or before it it is that single day's points
  (`(end - 1, end]`), and on or before the valuation date it has already
  delivered and is passed over with the reason. The near side interpolates the
  **rates** and accumulates them back from spot, not the running total: a
  straight line through two cumulative knots skips over the expensive day
  between them and reads it as the average one. Placing any of this needs a
  spot date, and a spot date needs a valuation date, which `feed.load_for`
  takes from the **book's clock** -- never from the machine, the same
  injected-clock rule as everywhere else. The file may state a spot date of
  its own (`PAIR,SPOT DATE,<date>`) and that wins: a publisher knows its own
  holidays and this tool's calendar may not, and a day's disagreement moves
  the tom-next onto the overnight. Derived or stated, which it was travels in
  `MarketFeed.notes` and is shown on the feed pill, for the same reason
  `derived` and `via` travel with a market level. Two consequences of putting
  the spot anchor in the knot list rather than in a special case: a feed with
  a **single** pillar now interpolates toward zero at spot instead of holding
  that pillar flat across the whole front, and a negative time answers 0
  rather than the front pillar's points. Both were the front-end rule the
  docstring already stated, applied in one place instead of three.
- **The workbook's CONFIG sheet is two columns: the pairs and the tenors.**
  A pair with the dollar on one side is marked on its own backbone; a pair
  without one is a **cross**, is never marked directly, and is broken into the
  two dollar pairs the market quotes by `cross.dollar_legs` -- EURJPY into
  EURUSD and USDJPY, EURGBP into EURUSD and GBPUSD. What is marked for it is
  the **correlation** between those legs, which is what a cross's
  `initial` / `long term` / `MR` cells have always meant. A leg nothing listed
  is added, because a cross cannot be built without both of them. None of this
  was ever a decision -- EURGBP has one sensible pair of legs -- and writing
  them out per cross was a chance to write one upside down, which flips the
  triangle's sign (§5 item 1). The old `BASE` / `COR` / column-per-cross layout
  still loads and **explicitly named legs win**: a sheet that says something is
  not second-guessed by a convention. Every derivation travels in
  `MarketData.notes` and is shown, for the same reason `derived` and `via`
  travel with a market level -- a pair that came out of a convention and one
  that was written down must not read the same.
  **A pair CONFIG names must have a sheet behind it**, and a sheet with no
  readable row is the same failure by the other route: both are reported by
  the reader. Deleting a tab and leaving the pair in CONFIG is an ordinary
  spreadsheet accident, and it used to be skipped in silence -- `volkit check`
  said *no problems found*, the book loaded looking complete, and the first
  call to reach that surface raised `EURGBP: no smile term structure; run
  calibrate() first`, which names neither the workbook nor the tab somebody
  deleted. `calibrate_smiles` says the same thing for a pair asked for **by
  name** with no quotes; a leg swept up on the way to a cross is not, because
  `load_all` builds those on purpose and the reader has already reported any
  sheet that is missing.
- **The server holds no screen state.** The browser owns the pricing legs, the
  listed panels and the analysis query, and posts each one whole. That is what
  makes `volkit listed` and `volkit analysis` reproduce a screen exactly, and
  it is why every endpoint is a pure function of its request plus the book.
- **No response may carry a non-finite float.** Python's `json` writes `NaN`
  and `Infinity`; `JSON.parse` refuses both, so one unavailable cell would
  take a whole response down in the browser. `webapp._finite` maps them to
  `null` on the way out.
- **Realized and implied share one clock.** Anything compared against a quoted
  volatility must be measured in the model's own volatility time
  (`history.volatility_time`), not calendar days and not a flat 252. A test
  pins it against `AtmCurve.integrated_vol`.
- **Units and signs are decided once per source, never per row, and a
  volatility is the number it was written as.** A historical sheet, a broker
  run, a listed quote table, a pasted curve and a market-maker target are all
  read in volatility points as written, whatever their level, and the same
  scale goes on the risk reversals and flies beside them. Per-column sniffing
  gets small risk reversals wrong. What no reader does is treat the **level**
  as evidence of the unit: a managed pair marks its at-the-money at a third
  of a point, and calling that decimals put it on the monitor at 35 and made
  a run that mixed it with a G10 line "ambiguous" and refused. Nothing is
  refused for straddling 1.0 any more, because there is nothing to be
  ambiguous about. `vol_unit='decimal'` is how a source in decimals is read,
  and it is something a person says. Where every level in a source sits below
  1.0 the reader says once, in its notes, that it read it as written and how
  to say otherwise -- it is the one reading somebody might have meant the
  other way. See MIGRATION.md F13 and F16.
- **A row that cannot be computed keeps its place and carries its reason.**
  Dropping it makes a short table look complete, and makes an all-failed table
  look empty. `carry_table`, `realized_table` and `triangle_table` all emit a
  blank row with the message instead.
- **A shortcut through the model is measured, not assumed.** The market-maker
  fit may read a delta off a SABR wing instead of solving the interpolation,
  but only for expiries where `marketmaker.anchor_gap` has *measured* the two
  to agree, and it re-checks at the answer. They do not always agree: the
  arbitrage-constrained SVI cannot pass through anchors that imply arbitrage,
  and USDCNY misses its own by 0.15 vol points. See §11.
- **A data file is read and closed, never held open.** Every workbook goes
  through `marketdata.open_workbook`, which copies the bytes and hands pandas
  a buffer; the feed CSV is read whole in a `with`. `pd.ExcelFile(path)` keeps
  the file open for as long as the reader lives, and openpyxl's workbook is
  full of parent/child cycles, so the handle outlived the call that made it --
  and on Windows that is enough to stop Excel saving the very sheet the tool
  had just read. These are somebody else's files and the tool only reads them.
- **One place reads a timestamp.** `timeutil.parse_datetime` tries the tabular
  formats first, unchanged, then ISO 8601 -- which is what the tool itself
  writes, in `/api/state`, in a session file, and out of a browser's
  `datetime-local` field. Three callers had each patched that in for
  themselves and had done it differently; the one that handled an offset
  *dropped* it and stamped the result UTC, reading `19:00+09:00` as 19:00Z.
  An offset is converted here, never discarded. Do not re-add a local
  `.replace("T", " ")`.
- **A tenor's unit may be spelled out, and a date may leave its year off.**
  Both are read by the one reader each, so every screen takes both. A tenor
  is a number and a unit and the unit is a word as readily as a letter:
  `1wk` *is* `1W`, `3mth` is `3M`, `10 days` is `10D`, and `normalise_tenor`
  still gives one canonical spelling per pillar, so a panel cannot list one
  expiry twice under two names. A date with no year on it is the **first**
  matching day on or after the reference date -- on 1-Sep-2026 `06 Nov` is
  6-Nov-2026 and `31 Aug` is 31-Aug-2027 -- because every date typed into
  this tool is an expiry, an event or a settlement, and all of them are in
  front of you. `29 Feb` is the one day whose next occurrence is not inside a
  year, and it is answered rather than refused.

  The reference date is passed in (`parse_datetime(text, today=...)`, filled
  from the book's clock, and `Clock.coerce_datetime` does it for you) and is
  **never** the wall clock: a box that reads `06 Nov` as a different day
  depending on when the machine is asked is not reproducible, which is the
  whole reason the clock is injected. With no reference date a year-less
  string is refused by name, saying that is what is missing. Purely numeric
  year-less forms (`06/11`) stay refused for the reason the ambiguous slash
  formats are absent from `_DATETIME_FORMATS`: day-then-month in one country
  and month-then-day in another, with nothing in the string to say which.
  And do not decide tenor-versus-date by counting characters, which is what
  `analytics` used to do -- four or fewer was a tenor, so `1week` was a date.
  Ask `parse_tenor`; it is the one that knows.
- **The events panel is typed in Hong Kong time; the model stays in UTC.**
  `EVTZ` in the page is the one declaration (fixed `+08:00`, no DST). Every
  `when` the server returns is UTC and is converted once on the way in
  (`evLocal`); every `when` posted carries the offset (`evPost`) so
  `parse_datetime` converts it back. No route, CLI command or session file
  changed: the conversion lives at the one edge a person types at.
- **A pricing leg's market is spot, the swap and the outright, and they hold
  one identity: `forward = spot + swap / pip`.** Two of the three are free
  and the third is arithmetic; `fwdsrc` says which of the swap and the
  outright the leg is holding, and `syncMarket` in the page is the one place
  the other is worked out. The feed fills all three -- and while it is still
  filling them the outright box takes the feed's **own** published outright
  rather than the sum of the two rounded boxes above it, because the boxes
  carry different precisions (a yen spot shows a tenth of a pip, the swap
  four decimals of one) and adding them up lands a digit off a published
  cross: EURJPY 1M off the sample file reads 162.864 that way and 162.865 as
  published. Only the outright is ever posted, so what is priced is the box
  that is on the screen and the server never has to choose between two
  spellings of one number. Beside them is the expiry, which takes a tenor
  (`1W`, `8d`) or a date in any of the spellings `timeutil.parse_datetime`
  reads and comes back holding the one standard date, so what is priced is
  what can be read on the screen. `pricing.resolve_legs` is that place: the expiry through the pair's own
  calendar, the level through `Book.market_level`, and the *same* reading the
  pricer does -- so what `Refresh spot` writes into a row cannot differ from
  what the row is then priced at, and a cross the feed quotes only through its legs
  fills its boxes rather than being refused, which is what asking the feed for
  the pair *by name* used to do here. `/api/legs` answers while somebody is
  typing and re-reads no file; `/api/feed/refresh` is the same reading after
  the file has been read again. A box the feed filled is refilled when the
  pair or the expiry moves -- the points are interpolated to the expiry, so a
  forward left behind is the wrong forward -- and a box somebody typed is not;
  the browser owns that distinction, as it owns the panel. **`Refresh spot`
  is the one control that crosses it**, and it crosses it whole: pressing it
  hands every market box back to the feed, typed ones included, and says how
  many legs it took back. There is no second button for the milder reading --
  there was, and it was the one nobody pressed, because somebody asking for
  the published market is asking for all of it and a level typed an hour ago
  is the one most likely to be stale. Emptying a box hands that box back
  without re-reading anything, which is the only per-box way back and so is
  the documented one, and moving a leg to another pair hands both back on its
  own -- a level somebody marked by hand belongs to the pair they marked it
  for, and 150.25 carried onto EURUSD is a silent zero with a decimal point
  in it. `OptionLeg.forward_points` is the other spelling, for a
  caller holding points rather than an outright, and it defaults to `None` and
  not to zero: nothing else can tell "said nothing about the forward" from
  "the forward is at spot", and those two want opposite things from the feed.
  The screen does not use it -- the swap box is converted where it is typed,
  the way every other edge of this tool converts once -- and a test pins that
  a leg never posts `points`.
- **The Results rows repeat no input box, because what was resolved is
  written back into the box it was asked in.** The expiry already worked that
  way; the strike and the option type now do too. `ATM` and `25d` are ways of
  *asking for* a strike, and once one has been solved on the marks the box
  holds that strike and the next price uses it -- so a delta strike does not
  quietly re-solve under a mark that has moved, exactly as a tenor does not
  re-read on a later morning. The box says what it was asked as (`strikeask`,
  shown as its tooltip), typing in it asks again, and moving the leg to
  another pair puts the **request** back rather than carrying 150.446 onto
  EURUSD. Showing the same number as an input above and an answer below was
  two places for one number to disagree, and for spot and the forward it was
  worse: the box was the input and the row read like a result. The
  **settlement date** left the Results rows under the same rule, in the other
  direction: it turned out to be an input -- the calendar's date is only its
  default, and a broken date is a term of the trade -- so it is a box, filled
  from the calendar, greyed while it is the calendar's, and emptied to hand
  it back. What is left of the market below is `market_source`, which is not
  a level but the screen naming which half of one is still the feed's; the
  screen has to **say** so (`spot_source` / `forward_source`, from the page's
  own `spotsrc` / `fwdsrc`), because it fills those boxes and then posts what
  is in them, and inferring provenance from a box being non-empty labelled
  every leg `typed` on a screen nobody had typed into.
- **The marking screen's vol query is the pricing screen's two boxes and one
  number.** `pricing.quick_vol` is that reading and `pricing.resolve_strike`
  is the one place a typed strike lands on the marks -- `ATM` to the
  delta-neutral straddle's own moneyness, `25d` to a solve on the interpolated
  smile, a number as written -- shared with `_price_leg`, which had its own
  copy of the same six lines. A strike read two ways is a strike that can be
  read two different ways, and `1M` understood differently on two tabs of one
  tool is the same failure. The forward is `Book.market_level`'s, at the
  expiry asked for, so there is no third box to leave stale and a cross the
  feed quotes only through its legs is placed from them with the triangle
  named. **Without a feed, `ATM` and a delta still answer** -- they are
  moneyness questions -- in `K/F` and saying so, the same rule as the smile
  chart's axis; an **absolute** strike is refused by name, because reading a
  level as a ratio is a wing nobody asked about. The **boxes keep the request
  and the answer line reports the resolution**, which is where this card
  deliberately parts from a pricing leg: a leg's strike is written back so a
  re-price cannot silently re-solve it, and on a query card re-solving under
  marks that have just moved is the entire question. Nothing resolved is ever
  written into a box, so no absolute strike can be carried onto another pair.
  **The wing is said in the strike box and nowhere else.** A bare `25d` names
  two strikes and is read on the call, as on the pricing screen; `25dp` and
  `-25d` are the put. A separate wing control was built here and taken out
  again: it was a second place to say one thing, and one that could be set to
  Call against a strike that had already said put -- the reverse of §11's rule
  that a side is settled once, where the row is built. At `ATM` or an absolute
  strike no side is reported at all, because the volatility there is one
  number for the call and the put (`quotes._settle_side`, the same statement
  from the other end). `/api/vol` belongs to the marking screen and
  `volkit vol` is the same call: `--strike` is that box and `--forward` an
  override, the latter being what moved a documented number (MIGRATION F14).
  **A strike and a delta are one point on the smile, and the card takes
  either.** The desk has whichever of the two the market gave it, so there is
  a Strike box and a Delta box -- but only ever one request: typing in one
  clears the other as it is typed, because a box that can be filled in and is
  then ignored is the silent zero this project exists to remove. What the
  request resolved to goes into the *other* box's **placeholder**, never its
  value, which is how the card shows both readings without breaking the rule
  above that nothing resolved is written into a box. The delta is
  `quick_vol`'s, not the page's: it is read under the pair's own
  `DeltaConvention` (premium adjusted for a USD-base pair, so a
  premium-adjusted delta-neutral straddle is a shade under 50 delta and a
  browser that assumed 50 would name a strike nobody asked about), and it is
  read at `F = 1` because delta is a function of moneyness alone -- which is
  what lets it answer for a pair the feed does not quote, where the strike
  itself cannot be placed. A request that did not name a wing is answered on
  the call, which is `resolve_strike`'s rule for a bare `25d` said once more.
- **A volatility is shown to two decimal places, said once.** `VOLDP` in the
  page, with `vnum` and `vsgn` reading it, and `anPct` / `anSgn` / `moCell`
  defaulting to it: the marks, the query, the risk reversals and butterflies,
  the daily vols, the kACE pillars, the listed comparison, the analysis and
  monitor columns, the fit misses and the quoted widths. The desk quotes vol
  to a hundredth of a point and the two decimals past that were two columns to
  scan across on every table. **What is typed keeps its full precision**: an
  overwrite box still holds four decimals, because a mark rounded to what it is
  displayed as would be a mark moved by being looked at -- and the command
  line, `/api/vol` and every CSV export are unchanged, so nothing that is
  compared or reconciled against another system lost a digit.
- **A cut selector offers what this desk marks on, and the model keeps all
  four.** `SHOWN_CUTS` in the page names TK and NY and `cutList()` is the one
  filter every selector goes through -- `STATE.cuts` appears once in the whole
  page, inside it. `atm.CUTS` is still the one declaration and `--cut LDN`
  still answers, because what the desk chose to stop looking at is a screen
  preference and not a change to the model. Filtering in `CUTS` instead would
  have taken the London cut out of the command line and out of the daily
  weights with it.
- **A screen shows a field only where the model reads it.** The pricing grid's
  rows carry the products they belong to: no barrier on a vanilla, no strike
  or ramp on a touch, no overhedge outside one. A box that can be filled in
  and is then ignored is a silent zero with a cursor in it. A row another leg
  needs keeps its place and shows a dot, so two legs never look like the same
  instrument, and the columns are a fixed width so a long premium cannot
  widen the leg beside it.
- **A panel is read before it is repainted.** Every screen here owns its own
  state and posts it whole (§4, the server holds none), so the fields *are*
  the payload: a spinner written into the div that holds them removes them,
  and the payload function then reads `.value` off `null`. The managed-band
  card did exactly that and no Apply ever reached the server. Build the body
  first, put progress somewhere that is not the form, and report a failure
  **beside** the form rather than over it -- a number with a typo in it is the
  ordinary way to reach the error path, and the box the typo is in has to stay
  on screen to be corrected.
- **A reload does not move the screen.** `boot()` runs again after every
  workbook reload, so every select is rebuilt with `fillSel`, which keeps what
  was chosen; the marking screen's pair, cut, interpolation and chart expiry
  survive, and so do its two disclosures, which the browser owns in
  `volkit.marking` like every other piece of panel state. The marks are
  discarded -- that is the point of a reload -- but putting a marker on a
  different pair is a change nobody asked for.
- **A card may be shut, but a mark may not be hidden.** The marking screen's
  ATM table shows the marked cut volatility and no longer shows the fitted
  curve beside it: the curve underneath a mark says nothing about the mark in
  the row next to it, and a table a volatility is read off should hold one
  number per tenor. Its **overwrite** column, and the whole **smile
  parameters** card, are disclosures shut by default -- neither is a marker's
  first question. What a disclosure may never take with it is the fact that
  something *was* marked, so a shut control counts the overwrites in its own
  heading and a shut overwrite column puts a dot on the tenor it belongs to.
  A mark nobody can see is the silent zero of §2 wearing a tidier screen.
- **Every text file is UTF-8, said once, in `paths`.** `read_text`,
  `open_text` and `write_text` are the only spellings; nothing calls
  `Path.read_text()` or `open()` on text and takes Python's default, which is
  the *locale* encoding -- cp1252 on the desk machine. Reading
  `volkit/web/index.html` with that default is what stopped the Windows build,
  at the test suite, with `'charmap' codec can't decode byte 0x81`, and the
  same default sat under `volkit.cfg`, `market_feed.csv` and the published
  feed. Reading strips a byte order mark (`utf-8-sig`, because
  Notepad and Excel both write one and it is not part of the first key or the
  first pair name); writing never adds one.

  **A file that is not UTF-8 is read anyway, and says so.** `paths.decode_text`
  is the one ladder: UTF-8 first, then UTF-16 when the file carries a UTF-16
  byte order mark (Notepad's "Unicode", unmistakable), then the machine's own
  ANSI code page (`paths.ansi_encoding` -- `cp936` on a Chinese Windows,
  `cp1252` on an English one). It stops there on purpose: `cp1252` decodes
  *any* byte sequence, so trying it on a `cp936` file always "works" and always
  produces mojibake. The one code page that is a fact rather than a guess is
  the one belonging to the machine that saved the file. Every fallback appends
  to `paths.ENCODING_NOTES`, which `cli.main` prints -- a file read in an
  encoding nobody chose is not an error, but it is not silent either, because
  the next thing it does is round-trip through this tool as UTF-8 and stop
  matching what Notepad shows. This was written after a `volkit.cfg` saved the
  way a Chinese Windows saves by default stopped the packaged exe at startup
  with a decode error instead of reading the workbook path in it.

  **The standard streams speak UTF-8, set in `paths.use_utf8_streams`**, and
  the *first* thing `launcher.main` and `cli.main` do is call it -- before the
  heavy imports, before the settings file is read, before anything prints. A
  redirected monitor table carries an arrow and cp1252 has no room for it; a
  settings file naming a workbook under a path written in Chinese is worse,
  because the launcher printed that path three lines before `cli.main` set the
  streams and the exe died with a traceback having done nothing at all. The
  error handler is `backslashreplace`, not `strict`, so a character a stream
  cannot take prints as itself escaped rather than ending the run.

  A test walks the source for the default spellings, so this cannot come back
  one call at a time, and another reads `launcher.py` to pin the stream call
  ahead of the first `print`.
- **A fit and a quote are two calls, and the marks travel between them, and
  keeping a fit keeps the marks it handed back rather than the raw numbers the
  optimiser stopped at.** A knob leaves in volatility points and comes back
  divided by a hundred, and `x * 100 / 100` differs from `x` in the last place
  for about an eighth of all values -- so `_knob_decimal` is the inverse of
  `_knob_points` in arithmetic and not in binary. Left alone, the book drifted
  one bit away from the marks the quote panel was posting and a price depended
  on whether *keep the marks* had been ticked: a nanovol, which is nothing to
  a market and everything to a screen that has to reproduce itself. One
  number, one spelling -- the book holds exactly what the panel shows.
  The market-maker screen's fit hands back `capture_marks` and the browser
  posts it with the request; `applied_marks` puts it on the surface for that
  one call and verifies the restore. The server still holds no screen state --
  the marks are the browser's, like the panel -- and a quote given none prices
  the surface as it stands and says so. See §11.
- **An event is weighted per currency; a pair adds its two legs and marks an
  adjustment on top.** `bump = w[leg1] + w[leg2] + adjust`, with
  `events.superpose` the one place the two legs meet (they add: a bump is a
  variance increment over twice the volatility, so bumps add to first order;
  root-sum-square is the rule for event *volatilities*, which a bump is not)
  and `events.EventEntry.resolve` the one place a pair works its bump out.
  Every `Event` carries the parts and the total and `bump == sum(weights) +
  adjust` always holds; an event typed as one number carries it as the
  adjustment, which is how a workbook without currency columns, an old session
  file and a `(when, bump, label)` tuple all still mean what they meant. The
  panel and the session file hold the parts in points and post the parts, and
  `events.event_entries` is their one reader. A total that disagrees with its
  parts is refused, never averaged.
- **The workbook's `EVENTS` sheet is the one place an event lives**, and
  `events.EventBook` is it in memory. One row per release -- identified by its
  time, in the workbook's own Hong Kong clock -- with a column per currency and
  a column per pair. A **currency** column is *shared*: every pair with that
  currency takes it, which is the whole reason the sheet has one. A **pair**
  column is that pair's adjustment and is its alone. A pair's schedule is
  **derived** (`EventBook.for_pair`) and never stored beside the table, so a
  weight cannot be one number on USDJPY and another on EURUSD -- which is
  exactly what a copy per pair column used to allow, and what the export then
  had to detect and cancel. `Book.events` is the session's copy of it and
  `Book.apply_events` is the one thing that puts it on a curve; `atm
  .set_events` is how the table reaches a curve, not how a mark is made.

  Events had dated rows on `PARAMS` once. A dated row left there is now
  **reported and not read**: two homes for one bump is how a weight comes to
  mean two things. The marking screen's Events card shows the sheet through one
  pair's eyes -- including a row that weighs nothing on it, because that blank
  cell is where its adjustment would be typed -- and its **Weights** card
  (`/api/events/weights`, GET and POST) shows the currency side whole; both
  post whole, and a weight that moves other pairs comes back as a note naming
  them. The session file carries the table once, as a top-level `event_table`,
  and a file written before it existed has one rebuilt from the union of its
  pairs' schedules, with two pairs disagreeing about a weight reported rather
  than averaged. `volkit session --to-workbook` writes the sheet whole.
- **Volatility points at the edges, decimals in the middle.** Everything a
  human types or reads -- a pasted quote, a knowledge-bank width, a curve
  parameter on screen -- is in volatility points; everything inside a model is
  decimals. Each boundary converts exactly once. A bank width read as a
  decimal turned a 0.28 market into a 28-point one.
