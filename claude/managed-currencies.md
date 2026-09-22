# volkit §6 — Managed / pegged currencies (`banded.py`)

Extracted verbatim from `CLAUDE.md` §6. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

The user corrected an earlier design that forced out-of-band probability to
zero: *"you can't force out of band probability to 0. The probability is real I
just need a possible adjustment to better model the jump risk."*

So the model is a **regime mixture**, not a bounded distribution:

- Peg-intact body: Beta on the band (U-shaped when a,b < 1, which matches the
  realised edge-seeking distribution).
- Break leg: a **hazard rate** λ, two-sided and asymmetric, with marked jump
  sizes and post-break volatilities.
- Breach probability is a calibrated **output**, and positive.
- Break risk is a **marked input**, never inferred from the at-the-money — a
  wider body and a higher hazard both raise the ATM, so that quote cannot
  separate them. **The wings can propose it**, and the calibration is two
  stages kept apart because they are identified by different quotes
  (`banded.py`, the section comment above `BREAK_PARAMS`): stage A
  (`_BodyFit`) is the exact forward-and-ATM solve for the Beta body; stage B
  (`_fit_break`) reads any subset of the break parameters off the 10d and 25d
  RR and fly by least squares, sweep then polish, the body profiled out
  exactly at every point. `calibrate_band_wings` is one tenor,
  `calibrate_band_term_structure` the whole curve under one shared regime with
  each tenor's own implied hazard reported beside it (flat: consistent;
  sloping: the jump sizes or the band are wrong), `fit_band_treatment` the
  surface-level entry behind the card's **Fit from the wings** and `volkit
  band --fit`. It **proposes and marks nothing**: the boxes are filled and
  Apply is the same Apply. `solve_hazard=True` is the one-parameter,
  one-instrument case of the same machinery -- the hazard against the strangle
  premium at one delta, bracketed -- and gives the number it always gave, to
  5e-15; the fly residual is the strangle *premium* over the strangle's vega
  for exactly that reason. **Identifiability is measured, not assumed**: a
  finite-difference Jacobian at the answer marks a parameter these quotes did
  not move as *not informed* and names a near-degenerate pair. Measured, the
  hazard against the jump size is *not* degenerate from strikes inside the
  band (the forward constraint moves the body with the jump) -- the jump sizes
  are held by default because they are a policy view, not because the quotes
  cannot see them. Residuals are in volatility (price gap over vega). A tenor
  no hazard can fit sits out with its reason rather than zeroing the shared
  ceiling.

Useful finding: the band alone gives a *negative* USDHKD risk reversal against
a quoted positive one. Most of the quoted skew is peg-break premium.

How much notice the surface takes of a band is `banded.BandTreatment`, marked
per pair and living on the surface beside `param_shifts`:

- `mode` — `warn` (default; lognormal prices, out-of-band strikes flagged),
  `off` (a deliberate marking that the range is not defended), `mixture`.
- the jump spec, an override of the band edges, and a `blend` against the
  lognormal smile.
- **The treatment is part of the smile cache key, and so is the level the
  feed puts the band at.** Two hazards are two smiles; a cache that could not
  tell them apart would serve the first answer for the rest of the session.
  The same is true of the forward, because a band is absolute and is placed
  against whatever the feed says *now* -- and the feed is re-read all morning
  (§15). With only the treatment in the key, a republished spot moved the
  forward column on the band card and left every probability beside it
  calibrated against the old one. `VolSurface._band_placement` is that half of
  the key; it is read for every band slice rather than only for the moneyness
  ones `band_for_slice` actually looks the feed up for, because a key that has
  to reproduce a decision made further down is a second place for that
  decision to live.
- **A blend strictly between 0 and 1 is a weighted average of two implied
  volatilities.** It is a marking convenience, is arbitrage free in neither
  model's sense, and warns.
- Bands load automatically (`Book.from_excel` → the workbook's `PEG_BANDS`
  tab, read through `configsheets.read_rows`), so a pegged pair is flagged on
  every screen rather than on whichever one remembered to call `load_bands`.
  `PEG_BANDS` is the *range*; the `BANDS` tab is the marking treatment applied
  to it, including an optional override of the edges. Two tabs on purpose.
- **`BAND` is only offered where it can work.** The page filters the
  interpolation list by `STATE.bands.pairs`: a named pair answers for itself,
  a screen spanning several (the monitor's tiles, a listed panel taking its
  pair from the contract) falls back to whether the book has a pegged pair at
  all. A leg or a panel restored from `localStorage` has its method put back
  to a legal one, because the browser owns that state and posts it whole -- a
  select that cannot show `BAND` while the state still says `BAND` is a
  setting the screen never showed and the server still receives. The server's
  refusal stays exactly where it is; this is not it moving.
- **The band warning reads the level the payout depends on**: the barrier for
  a touch, the strike for everything else. It read `leg.barrier` for every
  product, so a vanilla struck outside a band said nothing and a barrier left
  on a leg that was no longer a touch was checked instead of the strike.
- `VolSurface.band_for_slice` is the one place ratio space and absolute price
  space meet: the mixture is scale invariant, so the edges and the forward are
  divided by the same outright. No feed, or a forward the band does not
  contain, is a refusal with the reason.
- A delta strike on a band smile does not always come out of the fixed point:
  `v -> vol(K(v))` contracts only while the smile is gentle, and a band's
  wings fall away where the peg's support runs out. `SmileSlice
  ._delta_strike_bracketed` is the fallback -- delta is still monotone in
  strike -- and it runs *only* after the fixed point has failed, so no
  existing number moves. A delta the smile genuinely never reaches (hazard
  marked at zero, so compact support) says that, rather than "did not
  converge".

---

## The forward's own opinion (`pegcarry.py`)

Everything above calibrates the mixture to the **option** market. `pegcarry.py`
asks the same question of the **forward** market, which on a pegged pair is the
liquid one, and the two answers belong side by side.

Under the mixture the forward is the risk-neutral mean across all three
regimes, so solving that constraint for the break probability is algebra, not a
fit::

    b = (m - F) / (m - F * J)      J = w e^{+j_w} + (1 - w) e^{-j_s}

with ``m`` the peg-intact mean and ``b = 1 - e^{-lambda T}``. Two readings:

- **The ceiling.** Put ``m`` on the band edge the body walks toward — the lower
  edge for a net devaluation, the upper for a net revaluation — and you have
  the largest hazard the forward can support. `forward_hazard_bound` returns
  it in closed form with **no volatility input at all**, and a test pins it to
  the exact hazard at which `_BodyFit.build` stops building. It is therefore
  not a new model: it is the constraint `banded` already enforces, surfaced as
  a number and solved rather than bisected.
- **The attribution.** Hold ``m`` at spot and ask how much of the gap between
  the forward and spot the *marked* break regime actually accounts for.
  `break_share_of_gap` answers it as a fraction: near 1 the break explains the
  forward, near 0 the forward is the rate differential, negative means the two
  point opposite ways.

**Read the share, not the sign.** The equivalent spot-anchored *probability* is
kept as well, but it only looks wrong in one orientation. Against a devaluation
marking a carry-driven USDHKD forward gives an obviously impossible negative;
against a **revaluation** marking the identical forward gives a quietly
plausible large number instead. The desk book marks USDHKD with a net
revaluation break (jumps +2%/-7%, 60% weak-side, so ``J = 0.985``), so the
orientation that a sign test misses is the live one: reading the whole 1-year
discount as break premium gives 38%, which nobody believes and which nothing
flagged. The share says 13%, so 87% of that discount is carry — and it says so
in either orientation. `TestAttributionWarnsInBothOrientations` pins it.

**Never clamp the negative probability to zero either.** A clamp reports a
forward pointing away from the marked break as identical to one with no carry
and no fear at all.

The practical consequence, which the band card should say out loud: the mixture
still has to match the forward, so it pays for a carry-driven discount by
shifting the peg-intact body toward the strong edge — the `in_band_mean_shift`
already on the band panel. **That shift is carry, not a market view on where the
peg sits.** A genuine devaluation fear would move the body the other way.

Which tenor binds is the far one, and it is worth watching for an operational
reason as well as a marking one: on the live curve the 1Y USDHKD forward sits
about 190 pips above the strong edge of a band 1,000 pips wide, and `_BodyFit`
refuses a forward outside it. A few more months of the same carry stops that tenor calibrating
with nobody's view having changed, so the panel warns on a forward approaching
an edge before it reaches one.

Where this ceiling and `banded._hazard_ceiling` disagree, the tighter one is the
binding market — that one is the hazard above which the *at-the-money* can no
longer be repriced, and it needs the whole smile to find. Two different
questions, two different markets, deliberately not merged.

### The two legs, and the balance behind them

The differential `pegcarry` reports on its main table is the **net**, read off
the traded outright. That is the right number for pricing and the wrong one for
attribution: a forward walking toward an edge because the quote currency's rates
fell is peg stress, and one walking there because the base currency's rose is the
Fed. `rate_legs` splits it, and reads only what the feed **states**:

- `base_rate` — the anchor's own OIS curve (`USDOIS`).
- `quote_rate` — the quote currency's stated curve (`HKDOIS`), which
  `discount.py` keeps for precisely this and never discounts with.
- `quote_rate_implied` — what covered parity off the base curve and the traded
  forward says that rate is, through `F = S x DF_base / DF_term`.
- `basis` — the first less the second.

A non-zero basis is not an error. The implied curve is consistent with the
forward by construction — that is `discount.py`'s central invariant — so the
**stated** curve is the only place new information can enter, and on a defended
peg the gap between them is the funding premium that appears when balance sheet
is scarce. A CIP-consistent stated curve comes back at exactly zero, and a test
pins that.

`aggregate_balance` reads the HKMA's balance off the history workbook, from a
column named `BAL CLOS Index` (matched on letters and digits alone, so the
spelling is documentation rather than a pattern). It is the state variable the
whole defence runs on: **the Convertibility Undertaking triggers, the HKMA buys
the local currency, the balance drains, and the local rate has to rise** — which
is what moves the forward points. It leads that chain, so a differential that has
started to compress means one thing with the balance draining and another with it
flat.

The **level is not the signal** — it is a number of Hong Kong dollars and says
nothing alone — so a percentile of its own range comes back with it, along with
its move over `BALANCE_DAYS` calendar days.

**The column commonly runs behind the price columns beside it**, so the reading
is the last one *published* rather than whatever is in today's cell: blanks at
the end, and holes in the middle, are skipped back over. That fallback is only
safe if it is visible, so `date` (the reading's own day) comes back beside
`as_of` (the sheet's last day) and `stale_days` between them, and a gap longer
than `BALANCE_STALE_DAYS` is called stale in as many words. A forty-day-old
balance presented as current, on the one series whose entire value is that it
*leads* the rate, is the failure that reporting exists to prevent — and a
defence is exactly when it would mislead most. The recent move is measured in
**calendar days rather than rows** for the same reason: a column with holes in
it would otherwise make the window mean a different length on every pair while
the label kept saying one thing.

Two things about where it is read from. `history.py` used to discard any column
it had no reading of, with a note; it now keeps them in `PairHistory.extras`
under their own headings, because a sheet is somebody's file and a column on it
was put there on purpose. And `history._sheet_to_pair` needs six letters, so a
tab named for a **currency** (`HKD`) is skipped before the extras ever see it —
the balance has to sit on a pair-named tab (`USDHKD`). Where that has happened
the panel names the skipped sheets in its message rather than returning an empty
block, because an empty answer sends somebody looking for a column that is there.

### The relative-value grid is band-native on a pegged pair

`relvalue.py` used to *diagnose* a pegged pair rather than answer for one: it
emitted a warning saying the level, shape and history signals were most of the
declared weight and all read a volatility as the width of a lognormal. It now
answers. Where `_band_context` can price the expiry off the mixture, `level` and
`shape` stand down with a reason and the **`band`** signal is scored in their
place — the marked smile less the mixture's, at the same strike, in volatility
points and positive when rich. The richness becomes `band + carry`.

Three things to keep:

- **The at-the-money is the model's input, not a comparison.** `_BodyFit` solves
  the Beta concentration so the mixture reprices that very option, so the cell is
  zero *by construction*, shown and not scored — the same statement the `shape`
  signal already makes there, and for the same reason: a structural zero drags a
  score toward the middle exactly as hard as a counted absence would.
- **`band` is not a sixth weight.** It is absent from `WEIGHTS` and takes the
  combined weight of the two it replaces, through `cell_weights`. A sixth dial
  would leave 0.50 of permanently unavailable weight on every cell of every pair,
  and a constant deduction is not information.
- **The forward's opinion is not duplicated here.** `pegcarry` answers the hazard
  question off the swap points; the grid names that read-out in its warning and
  leaves it there. Two screens answering one question in two arithmetics is how
  they drift apart.

The signal is informative in the wings and structurally silent at the money,
which is the right shape: given the ATM, the forward and the marked break, the
mixture's content *is* the smile.

---

## The band as a process (`targetzone.py`)

Everything above is a **terminal-distribution** model: the Beta concentration is
solved from the at-the-money, one free parameter per expiry, carrying no memory
of where the spot sits in the band today or how fast it has historically moved
across it. That is enough to price a smile and not enough to have an opinion
about one.

`targetzone.py` models the band position `x = (S-L)/(U-L)` as a **Jacobi
(Wright–Fisher) diffusion**:

    dx = kappa (m - x) dt + sigma sqrt(x (1 - x)) dW

which is not an arbitrary choice. Its support is *exactly* the band with no
reflection bolted on; its diffusion coefficient vanishes at both edges, which is
Krugman's smooth pasting in one line; and **its stationary law is exactly
Beta(a, b)** — the family `banded` already prices with — at

    a = m s,   b = (1 - m) s,   s = 2 kappa / sigma^2.

So this is a *prior on an existing parameter*, not a second model. Note it is
Jacobi and **not reflected-OU**, which the literature usually reaches for: a
reflected OU's stationary law is a truncated normal, which is bell-shaped
whatever its parameters and so cannot represent the edge-seeking the note above
describes. Jacobi is U-shaped exactly when `a, b < 1`.

Both conditional moments are closed form, so today's position gives the body at
**every horizon** from one estimate — a point mass now, the stationary Beta
later — which is the term structure `banded` refits per expiry.

Three things to keep:

- **Only the spread is handed to the pricer.** `_BodyFit.build` re-centres on the
  forward, so a disagreement is about the *shape* of the band and never its
  level. The forward is a price; this module has no business overriding it. A
  test pins that the built body's mean is still the forward.
- **`kappa` is weakly identified and the module says so.** `sigma` is recovered
  sharply from the same series; `kappa` is not, which is the usual asymmetry for
  mean-reversion speed. The concentration is therefore quoted with a range, and
  the panel warns when the standard error exceeds the estimate, and again when
  the fitted centre sits within `EDGE_CENTRE` of an edge.
- **The first estimator was wrong and is worth not re-deriving.** An Euler fit
  with GLS weights `1/(x(1-x)dt)` looks right — those *are* the reciprocal
  variances — but the weights explode exactly where a defended peg spends most
  of its life, so a handful of near-edge observations took over the regression
  and `kappa` came back from half to twice its true value. It recovered
  bell-shaped parameters perfectly, which is what made it convincing. The fit
  now uses the **exact** conditional mean, which is unbiased at any spacing.

### Touches know the edges are defended (`exotics.band_touch`)

A lognormal gets a touch on a pegged pair wrong in *both* directions: inside the
band it overstates the chance of reaching a level, because its diffusion does not
die as it nears an edge; outside the band it prices a break-only event as
ordinary diffusion. A double no-touch struck on the Convertibility Undertakings
is exactly that trade.

`band_touch` walks the Jacobi path while the peg holds and switches to a
lognormal at the marked post-break volatility when a Poisson break fires. The
result on USDHKD is not a refinement — **a one-touch on 7.75 prices at 0.037
against 0.55 lognormal**, and the peg-intact leg is exactly zero, because a
diffusion whose volatility vanishes at the edge cannot reach it. Only a break
can.

It is Monte Carlo, because first passage under a state-dependent diffusion has no
closed form, and the Brownian-bridge correction freezes the coefficient at each
step's start — exact for the lognormal leg, an approximation for the Jacobi leg
that errs toward *under*-counting touches near an edge. `pricing.py` uses it only
under `BAND` and only when a zone has been measured; otherwise the lognormal
answers and `pricing_method` names which engine ran.

---

## Where the peg work lives on the screens

One question — *what is this band worth* — is asked of three different markets,
and all three answers belong on the **Vol marking** screen, because each is
evidence about something that is **marked**:

| card / action | market it asks | what it is evidence about |
|---|---|---|
| Managed band → *Fit from the wings* | options | the break regime (hazard, jumps) |
| Managed band → *Estimate from history* | spot series | the Beta body's concentration |
| Swap points card | forwards | the hazard's ceiling, and carry vs break |

None of them marks anything; each proposes or measures, and the desk applies.
Putting them together is the point: a marker deciding a treatment wants the
three readings side by side, and two of them contradicting the third is the
useful case.

The **Analysis** screen holds the *verdict* rather than the evidence: the
relative-value grid's `band` signal, which scores the marked smile against the
mixture at the marked hazard and is what says rich or cheap. The **Pricing**
screen holds the consequence: a touch under `BAND` prices off the Jacobi path
and names its engine in the `Method` column.

All four of the band routes — `/api/band`, `/api/band/fit`,
`/api/band/dynamics` and `/api/peg-carry` — are owned by the `marking` screen,
so a build without that tab refuses all of them by name (§14). `/api/peg-carry`
was unowned when it was first added and therefore answered in a build the tab
had been excluded from; `TestScreens
.test_every_dispatched_route_is_owned_by_a_screen_or_is_shared` now pins the
whole set against an explicit list of the routes that genuinely belong to no
screen.

### The band card is a window

The band work grew to a form, three actions and three read-outs, and stopped
fitting the column it sat in. It now opens in a **window** — the same `.modal` /
`.sheet` / `.sheethead` / `.sheetbody` idiom as the contour panel and the Config
window, opened by `#bandopen`, shut by its Close button, the backdrop, or
Escape (which tries the contour panel, then this, then Config).

What stays on the marking screen is `#bandtrigger`, and what it carries is not
decoration. **§4: a card may be shut, but a mark may not be hidden.** A window
that is closed is a card that is shut, so the trigger shows the effective band
edges, the treatment as `BandTreatment.describe()` puts it, and a count of
anything the band wants said. A marked hazard invisible behind a closed window
would be the same failure as a shut control that does not count its own
overwrites.

Two things the move taught, both pinned by tests:

- **The window re-reads on open.** The feed is re-read all morning (§15) and a
  band is placed against whatever it says *now*, so a window opened an hour
  after the pair was chosen would otherwise show breach probabilities
  calibrated against a forward that has moved. `bandOpen` awaits `loadBand`.
- **A duplicate id is invisible to the existing id test.** Carrying the card's
  own heading into the window left `bandstatus` and `bandedges` defined twice —
  once in the window head, once in the card — and `$('#id')` silently binds to
  whichever comes first, leaving the other never written. The old test asks
  whether a lookup *resolves*, which a duplicate passes. `TestWebAssets
  .test_no_element_id_is_used_twice` asks the other question.

The window holds both cards, because they are one decision: the treatment form
and the wings fit, then the swap-points read-out under it. The screen behind
keeps one line.
