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
  two legs and ignores the change of measure between their domestic
  currencies. Both are stated in `moments.py` and bounded by the reported
  noise floor; neither is corrected for.
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
