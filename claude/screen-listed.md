# volkit §8 — Exchange traded options (`listed.py`)

Extracted verbatim from `CLAUDE.md` §8. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

Added as a third UI tab. Panels are owned by the browser and posted whole, so
the server keeps no panel state and `volkit listed` reproduces a screen exactly.

- The fit is a **least squares over N strikes**, not the three-quote solve in
  `sabr.py`. Alpha is profiled out at each `(rho, nu)` by a bounded scalar
  search inside a bracket from `alpha_roots_at_forward`, keeping the outer
  problem two-dimensional so the whole box can be swept before polishing. Same
  no-starting-guess discipline as `sabr.calibrate`.
- **The contract is free text, and a typed one says so.** A desk trades more
  listed contracts than any table shipped here will hold, so `UNDERLYINGS` is
  a set of *suggestions* offered by the box, not the set of legal answers. A
  code it does not hold is taken as typed and carries `known=False`: its pair,
  strike direction, scale and contract size are the panel's own, nothing is
  inferred from the name, and every screen marks it *typed* — a typo (`6R` for
  `6E`) must not read as a contract that merely has no mapping. What is still
  refused is a code with no shape, so a mis-pasted quote row cannot become the
  name of a contract. The dropdown it replaced is why: every contract missing
  from it had to be entered as `CUSTOM`, two `CUSTOM` panels on one screen
  cannot be told apart, and a position line naming either was refused as
  matching two panels with no field left that could settle it.
- **Inversion is the trap.** `6J` is USD per JPY. Strikes reciprocate onto
  USDJPY and the wings swap sides; lognormal vol itself is invariant. All
  comparison is done at **matched physical strikes** — never by matching
  deltas or negating a risk reversal, which is how §5 item 1 happened. The
  reported RRs are the book's own delta strikes read off both curves.
- **Any of `alpha`, `rho` and `nu` may be given rather than fitted**, and a
  given one is held everywhere: the sweep visits no other value of it and the
  polish does not carry it as a variable, so what comes back is the best curve
  through the quotes *at* that value. It is substituted from the pin and not
  read back out of the optimiser's vector -- alpha travels through a logarithm
  there and `exp(log(a))` is not always `a`, and a number somebody typed must
  come back as the number they typed. The count rules follow the **free**
  parameters, not SABR: two held parameters leave two quotes sufficient, and
  "an exact interpolation" is `n == len(free)`. Hold all three and nothing is
  fitted; that says so rather than reporting a convergence it never attempted.
  Every held parameter is marked where it is shown, on the screen and in the
  CLI, because one that was typed is otherwise indistinguishable from one the
  market implied.
- The parser reports every inference and every rejected line. A table
  straddling 1.0 is refused, not guessed.
- The arbitrage check runs in **moneyness, per unit of forward**. In raw
  strike units the second difference of a yen future's prices is small enough
  that rounding reads as arbitrage; the test pins this at four forwards.

## Positions and aggregated risk

A second panel on the same tab: what is owned, against the panels above. A
position line is `contract, expiry, strike, C/P, contracts` (or the short
layouts, or a header row), and `PositionPanel.run` prices each one against the
fit panel it names. Posted whole like everything else here, so
`volkit listed --positions` reproduces it.

- **A position names a panel, and everything a greek needs is that panel's** --
  the forward, the expiry, the volatility at the strike. The one parameter a
  fit never needed is the **contract size**, which is why it was added as a
  field on the *fit* panel (defaulting to the contract's standard: 125,000 for
  `6E`, 12,500,000 for `6J`) rather than invented in the positions panel.
- **Two sets of greeks, and the premium is the same in both.** Black-Scholes
  is the closed-form Black-76 sensitivity at the option's own volatility with
  that volatility *held fixed* as the future moves -- what a Black-Scholes
  greek is, and what an exchange's own risk file will agree with. Smile is the
  same position revalued on the fitted SABR curve, with the forward bumped
  *inside* the parameters so the curve travels with the future. Both read one
  volatility at one strike, so the entire difference is in the sensitivities.
  `black.theta`, `black.vanna` and `black.volga` were added for the first
  column and each is pinned against a finite difference of `black.price`.
- **Vega is a lift of the whole curve, measured at the forward.** Alpha is
  scaled, the resulting at-the-money move is *measured*, and the revaluation
  is divided by it -- no solve, and the number is per one point of
  at-the-money volatility however alpha happens to map onto it. At `K = F` it
  is therefore exactly the Black-Scholes vega, and a test pins that.
- **Three aggregates, because three different things add.** *Per panel*,
  every column adds. *Per contract*, every column still adds — the panels
  under one code are the same contract at different expiries, and that is the
  number a desk means when it asks how much `6E` it is running. This is what
  §8 always said and what the code did not do: it aggregated per *panel* and
  jumped straight to money, so a book of one contract over four expiries had a
  futures-equivalent delta nowhere. Two delivery months are not the same
  future, so the row says that total is a net position and not a hedge ratio.
  *Across contracts*, only money adds. `ADDITIVE_GREEKS` is the one
  declaration of which columns those are.
- **Money adds only within one settlement currency.** The premium comes out of
  Black-76 in the currency the *listed strike axis* is quoted in, which
  `ListedUnderlying.premium_ccy` derives from the pair and the inversion —
  the term currency as the pair is written, the base currency when the
  contract is the reciprocal. Every CME contract works out as USD, which is
  why this was a single total for as long as the contract came off a list; a
  typed contract need not, so the totals are struck per currency and the
  all-in row is *blanked* rather than left showing a sum of euros and dollars.
  A panel with no pair has no derivable currency: it joins the total and the
  screen says it was assumed to match.
- **A line that matches no panel, or two, keeps its place and says which.** A
  position priced against the wrong month's curve looks perfectly ordinary,
  which is the one thing that may never be guessed at. The refusal offers the
  **label** as well as the contract and the expiry, because two panels may
  legitimately agree on both of those and the label is the one field that is
  always free to differ. A panel that will not
  fit reports its own message on the lines that name it and takes none of the
  others down.
- **A comma is a column boundary**, as in a broker run (§11), so
  `_detect_position_delimiter` believes any comma at all -- unlike the quote
  table's rule, which needs every row to agree because `1,425.00` is a strike.
  A size written `1,000` in a comma paste is then two columns and its row is
  refused with the reason. The **layout is decided once from the whole table**
  (the modal field count); a row of a different width is skipped rather than
  read on its own width, which would move a quantity into a strike the first
  time somebody left a cell blank.
- **A theta window past the expiry blanks the smile theta with the reason.**
  There is no revaluation to take the decay from, and the closed-form column
  beside it is still reported.
