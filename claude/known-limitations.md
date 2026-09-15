# volkit §7 — Known limitations (flagged, not fixed)

Extracted verbatim from `CLAUDE.md` §7. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

- **Same-day expiries cannot be priced.** `cut_vol` normalises by whole
  volatility days; today's expiry has zero, so vol is zero and Black rejects it.
- **No discount curve.** All premiums are undiscounted forward values.
- **Half-day holidays** (Christmas Eve, day after Thanksgiving) are full days.
- **The band model needs a forward feed.** It is now a UI interpolation method
  (`BAND`, §6), but a band is absolute and the surface works in strike/forward
  ratio, so placing one needs the outright forward at the expiry. Without a
  feed for the pair it refuses and names the feed rather than guessing a level.
- **The cross triangle for RR and fly assumes a Gaussian copula** between the
  two legs unless `CROSS_DEPENDENCE` marks a vol-vol correlation and a
  correlation vol for the cross, and ignores the change of measure between the
  legs' domestic currencies either way. The marked dependence is itself a
  choice: lognormal variance regimes sized by each leg's own kurtosis, a
  two-point correlation, and no link between the correlation and the regimes
  (correlation rising *in* stress is not modelled). The correlation's link to
  the cross's *direction* is modelled (`corr spot corr`): a skew-normal lean of
  the two correlation states, which needs a correlation vol to act, carries at
  most 0.80 of correlation, and cannot reach a risk reversal beyond what a full
  lean of the marked corr vol gives. The regimes themselves do not lean (the
  legs' own skews already carry their vol-spot).
- **The measured corr spot corr is the market's correlation moving**, not a
  realized one: daily changes in the correlation three quoted ATMs imply, which
  carry whatever the smile does to an ATM as spot moves along it, and quote
  noise the autocovariance correction only partly removes (capped at x2). It
  needs the cross's own sheet. The realized alternative -- rolling-window
  correlations against the window's cross return, consistent with corr vol --
  was measured in simulation and could not recover a known lean on two years
  of history, so it is not offered. The daily measure is also a statement about
  daily increments applied to a lean over the option's life, which holds for a
  diffusive correlation and not for one that jumps. All stated in `moments.py`;
  the change of measure is not corrected for.
- **Measured dependence is physical and a premium is borrowed.** The realized
  vol-vol correlation and correlation vol carry no risk premium; the
  relative-value triangle adds none unless another cross's is named, and a
  cross with no market of its own has no premium of its own to check the
  borrowed one against. The vol-vol correlation is daily `dlog ATM`, which is
  the model's log-variance shock only if the regimes are built of those
  shocks; the correlation vol is net of sampling noise but thin on long tenors
  (two years holds two 1Y windows, which is refused), and both depend on the
  regime the history window happens to cover.
- **Fair value is a first-order break-even**, not a valuation: it ignores the
  convexity of the gamma P&L in realized volatility and assumes the surface
  does not move.
- **Realized moments are projected onto a tenor assuming independence**
  (`skew/sqrt(n)`, `kurtosis/n`). Real returns are not independent; the raw
  daily figures are reported beside the projected ones for that reason.
- **Listed-option comparisons are not adjusted** for American exercise on
  exchange settlement volatilities, for futures-vs-forward convexity, or for
  the exchange settlement time not being an FX cut. All three are reported in
  the panel and in the docs rather than silently absorbed.
- **The relative-value carry signal stretches the fair-value break-even out
  into the wings.** The gamma against theta is read as `(h/T) * vega *
  (sigma_R - sigma_I)`, which is first order at the at-the-money and rougher
  at a 10 delta strike, where the option's gamma over the horizon is not that
  share of its whole life. Stated in `relvalue.py`, not corrected for. What
  *is* corrected for is the option's own delta: see §9's `carry_hedged`.
- **The relative-value shape signal inherits SABR's lack of mean reversion.**
  The comparison smile is built from the *measured* `(rho, nu)`, and a measured
  `nu` falls away at long tenors because real volatility mean-reverts and SABR
  does not. A long-dated wing can therefore be scored rich against a
  comparison smile that is flatter than the market would ever be. The grid
  warns, the ρ/ν card says the same thing, and neither corrects for it.
- **The managed-float reading is a heuristic on measured numbers, and it is
  not the authority on anything.** A hard, defended band is a *policy fact*
  and is marked on the workbook's `PEG_BANDS` tab (§6); `relvalue.suppressed_diffusion`
  only raises a hand on a pair whose carry and realized volatility have the
  shape. It deliberately takes **two** conditions, because the obvious
  one-condition version is wrong: read as `|c| / sigma` alone, USDJPY on a
  five point rate differential and ten volatility points scores 0.53, right
  beside USDCNH's 0.50, and USDJPY is not managed in any sense. The second
  condition is a low realized volatility in absolute terms
  (`MANAGED_VOL_CEILING`), which is the suppressed diffusion itself rather
  than a consequence of it. A high-carry, high-volatility pair -- USDTRY at 35
  and 25 -- is outside it on purpose: its diffusion is not suppressed, it is
  merely expensive. Both thresholds are marking judgements and the
  measurements behind them are reported whether they trip or not.
- **The carry weight is not tapered by the regime, on purpose.** `regime_z`
  says which side of the carry-dominance line a tenor is on, and the row, the
  carry signal and the CLI all say so, but the weight stays where the desk put
  it. A score that quietly reweighted itself would be a different statistic on
  every row with nothing on the screen to say so -- the same rule as §11's
  knowledge bank, where a width that no rule matches gets no width rather than
  an invented one.
