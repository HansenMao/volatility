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
- Bands load automatically (`Book.from_excel` → `files/bands.csv`), so a
  pegged pair is flagged on every screen rather than on whichever one
  remembered to call `load_bands`.
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
