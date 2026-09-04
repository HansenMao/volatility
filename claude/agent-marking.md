# volkit §18 — The marking agent (`remarks.py`, `marking.py`, `consult.py`)

Extracted verbatim from `CLAUDE.md` §18. Section numbers throughout this repository's docs refer to
CLAUDE.md's original scheme and are unchanged. CLAUDE.md carries the one-line rule and points here
for the reasoning behind it. Read this file when working in the area above.

A second agent, and a different problem from §17's. The quoting agent answers
*what do I show*; this one answers *where should the surface be*, which is the
question the marking screen's two fitters leave to a person.

**The fitters do not change.** `fit_atm_curve` stays a cold fit,
`tune_smile_shifts` stays a curve-wide additive shift under a hinge, and every
rule in §11 stands. What a marker actually agonises over is the judgement
*around* the fit -- which knobs to free this morning, whether four targets can
determine four parameters, whether to touch the wings when only the
at-the-money was quoted -- and then whether to take what came out. That
judgement is what this agent makes, and the last part of it is what it learns.

Two ways in, like the quoting agent. `volkit mark propose|confer|learn|journal|
record` on the command line, and a **card inside the market-maker tab**,
beside the fit it plans. Both belong to the **mm** screen: the fit this agent
runs is `marketmaker.fit_atm_curve` and `tune_smile_shifts`, the fit panel's
own, so a build without that tab has nothing for it to plan, and the command
belongs to it for the same reason. Excluding the market-maker tab takes both
agents.

## The card, and how the two agents are tied to the two buttons

The tab has two buttons because it has two jobs (§11), and each agent is
wired to one of them:

- **The marking agent is on the fit.** `marking.MarkPanel` (`/api/mm/mark`)
  reads the *fit panel's own fields* -- the paste, the target source and
  text, the conventions -- through the same `marketmaker.panel_from_request`
  the Fit button uses, and answers how it would run that fit and what came
  out. It has no market of its own on purpose: a proposal about some other
  fit is not an answer to the question on the screen. What it adds is the
  marker's judgement -- `choose_knobs` lets it pick the free set (a rule from
  the target count and what the quotes inform, a learned pin from the
  journal), or it takes the panel's ticks as the caller's -- and the learned
  nudge. Its answer carries `marks` in exactly the shape `Panel.run` hands
  back, so **Accept** puts the proposal where a fit's answer goes: the
  browser's one holder for the marks the quote stands on (`HELD`, filled by
  the fit or by an accepted proposal, never both at once), and the quote
  sheet's note names which. **Take the plan onto the fit** writes the plan
  into the fit panel's knob boxes and runs Fit, so the desk can adjust and
  press Fit again; **Record my fit as the edit** then journals the fit's marks
  beside the proposal, which is the row §18 says is worth the most. The
  verdict buttons vanish when the paste has moved on since the proposal,
  for the same reason the quoting agent's columns do.
- **The quoting agent is on the quote.** Its card compares widths and proposes
  nothing, as before; the link is the **widths from the archive** switch on
  the toolbar, which puts the archive on the quote panel's width ladder --
  **bank, then archive, then the typed fallback, then no price**, the same
  ladder and order as `agent.run` -- and every row names the rung. Off by
  default: the archive is evidence about the market, and a desk that has not
  yet convinced itself of it should not find it under its prices. The
  evidence settings are the quoting agent card's own boxes (half-life, minimum
  evidence, lookback) read by both, so the card and the quote never disagree
  about what the archive holds. The archive's *level* rides on the row as a
  flag and is applied to nothing (§17's rule). `volkit mm --archive-width`
  is the same switch.
- **`/api/mm/mark/record` is the only route on the card that writes**, and it
  writes to the journal. `accepted` records the proposal as the outcome,
  `rejected` records the start, `edited` needs the marks the desk ended on
  and refuses without them -- an edit recorded as the proposal would be the
  agent agreeing with itself. `apply` puts the recorded marks on the loaded
  book, the fit panel's *keep the marks* decision made here, and it reads
  that same checkbox. Answering the same proposal twice is one instance,
  said rather than raised: the journal is content-addressed.
- **Wing parameters are freed only where a quote reaches them.** The plan
  runs `marketmaker.informative_params` over the wing quotes before it counts
  them: a single 25-delta risk reversal frees `slog25`, not `slog10`, because
  the ten-delta parameters do not enter the 25-delta anchor and the tune
  refuses a parameter nothing informs. Found by the CLI smoke of the card,
  which had freed the first name on the list.

## What it learns from (`remarks.py`)

- **An instance is a diff of two snapshots, not an instrumented control.**
  `session.capture_pair` already photographs every knob, so a re-marking
  instance is a before, an after and a subtraction. Nothing in the marking
  screen reports anything, nothing is forgotten when a control is added, and a
  session file from last month can be turned into instances retroactively.
- **A verdict is worth more than a diff.** `unprompted` is somebody marking;
  `accepted` / `edited` / `rejected` answer a proposal and carry the one thing
  a diff cannot -- what the tool would have done, beside what the desk did, on
  the same morning. An **edited** proposal is the most valuable row in the
  file, and it is the whole reason the agent asks rather than only watching.
- **An absent smile shift is a zero; an absent tenor overwrite is not.** One is
  "they moved it", the other is "they left the curve to speak", and counting
  them alike loses exactly the decisions a marker thinks hardest about.
- Append-only and content-addressed, like the quote archive: a morning
  re-marked twice must not become two instances of a desk that likes moving
  that knob.

## What "learned" means (`marking.py`)

A desk re-marks a curve a few times a day. Over a month that is a few dozen
instances -- enough for a handful of scalars with error bars, nowhere near
enough for a function. So:

- **Tendencies, not a policy.** Per knob: has this desk been given the chance
  to move it and declined every time (`MIN_INSTANCES`, 5); how far does it
  typically move it; how often is a proposal taken as it stood; and does the
  desk land systematically off the fit. Every one carries its count and
  refuses to say anything below the floor.
- **A correction must be a tendency and not a scatter.** The median of six
  corrections is a number whatever those six were; it is evidence only when
  they agree. So a bias is applied only when `|median| > BIAS_SIGNAL x spread`
  (spread being half the interquartile range, which one outlier cannot set),
  and otherwise the row says *this desk lands on both sides of the fit here*
  and nothing moves. That single test is what stops the agent learning the
  desk's noise and quoting it back with confidence.
- **A correction is capped at `CORRECTION_CAP` of what the fit itself moved**
  (half). A nudge on a fitted number is a nudge; a nudge that can exceed the
  fit is a second, unexamined fit with a smaller sample behind it. Where the
  fit barely moved a knob, the correction is skipped and says so.
- **Age is not a weight here**, unlike the quote archive. A width is a fact
  about a market that moves; how a desk marks is a fact about the desk, and a
  habit from three months ago is still that desk's habit. What ages out is the
  window (a year).
- **A rule and a learned reason are labelled apart** in the trace. A rule is
  true of the model -- four targets cannot determine five parameters. A
  learned reason is true of this desk and always carries the instance count.
  Somebody disagreeing with the second must see immediately that it is the
  second.
- **Every proposal says how much it learned from, including none.** A proposal
  that quietly had nothing behind it and one built on a year of instances must
  not read the same.
- **`marked()` is the context manager everything runs inside**, and the
  restore is *verified* rather than assumed -- a surface left half-marked by a
  proposal nobody accepted, priced off all morning, is the worst possible
  outcome of a tool whose whole job is marking. A fault-injection test pins
  the guard.

## Rules of thumb as a prior (`rules.py`)

Everything above learns from the journal and from nothing else, so the agent
says nothing useful for the first month. But a desk already knows things the
journal does not -- the back end lags broker moves, risk reversals are moved
less often than the at-the-money, marks land on the quarter, a desk is readier
to raise vol than to cut it into a bid. Writing those down turns the first
month from *accumulating toward a floor* into *falsifying a stated belief*.

**A rule of thumb is a third kind of reason, and is labelled as one.** A rule
is true of the model, a learned reason is true of this desk and carries its
count, and a rule of thumb is neither -- true of markers generally, not yet
true of this desk. `rules.LABEL` (`rule of thumb`) is the one spelling, the
page's tag map keys on it, and a `Correction` built with a prior in it reports
`learned + rule of thumb` and carries the decomposition as `prior`. Without the
third label the agent either dresses a hunch up as a model constraint or quotes
it back as though the desk had taught it.

**It is seeded into the sample, not inferred beside it.** At `learn` time each
rule is expanded into `weight` synthetic corrections placed symmetrically about
its `value` so that `_median` returns `value` and `_spread` returns `spread`
exactly -- for `weight = 4` that is `[v-s, v-s/2, v+s/2, v+s]`, and a test pins
the placement at each allowed weight, because `_spread`'s quartile indices pick
different rows at each `n`. Everything downstream is untouched: `bias()`,
`BIAS_SIGNAL`, `CORRECTION_CAP`, `describe()`. Medians and interquartile ranges
do not compose analytically the way means and variances do, so seeding the
sample is the one clean way to blend a prior into statistics of this shape, and
it leaves no second code path to keep in agreement with the first.

The failure mode is worse than the one `BIAS_SIGNAL` was built to stop: a prior
that never gets falsified is the author's own hunch recited back with the
desk's confidence attached, and it *looks* like evidence. Three guards:

- **`weight` is clamped at `MAX_PRIOR_WEIGHT` (5) and floored at 2.** A rules
  file asking for more is a load error and not a silent trim; below two there
  is no spread and `bias()` refuses anyway. The outvote boundary is **16**, not
  15: `_spread` reads its upper quartile at `ceil(0.75 (n - 1))`, so at weight
  5 with fifteen real corrections that row is still the first pseudo-correction
  and a rule half a point away holds the spread open. Fifteen outvote a rule
  inside the desk's own range; the sixteenth outvotes any rule. Tests pin both,
  and `MAX_PRIOR_WEIGHT`'s comment carries the arithmetic.
- **A prior shapes the size of a nudge and never authorises one.** `bias()`
  applies a third test after the floor and the spread, whether or not a prior
  is present: at least `MIN_REAL_CORRECTIONS` (3) *real* corrections on the
  median's side of zero. Zero real corrections means no correction is applied
  however confident the rule -- it is still printed, as a rule of thumb the
  agent is not yet willing to act on. That is also what stops a `weight` of 4
  clearing `MIN_CORRECTIONS` on its own. The real corrections travel on the
  tendency (`real_corrections`, with `correction_real_n` and `correction_real`
  read off them) so nothing else has to know which rows were seeded.
- **Every rule-shaped line decomposes**, e.g. `+0.15 = +0.10 rule of thumb,
  +0.05 desk (n=7)` -- `_median` called twice, once on the seeded list and once
  on the real-only one. And "every proposal says how much it learned from"
  extends to *from 7 instances, plus 3 rule-of-thumb pseudo-instances*, never
  one blended count.

**The contradiction register.** The highest-information row in the file is a
rule of thumb the desk edits away every single time. For each rule `mark learn`
reports the real-only median and the count of real corrections on the far side
of `value`, and prints the rule **contested** when that median has the opposite
sign with `CONTESTED_N` (8) or more real corrections behind it. Nothing happens
automatically -- no auto-halved weight, no silent retirement. That would be a
second unexamined mechanism with a smaller sample behind it, which is the
mistake this section refuses to make everywhere else. It is flagged, and a
person edits the file.

**The file** is TOML read with stdlib `tomllib` -- hand-editable by a trader,
no new dependency, and no write path is needed. `mm_rules.toml` beside the
workbook, or in `config/` for a house default; `files/mm_rules_sample.toml` is
the sample and is never loaded. A file that is there and wrong is refused whole
(`RulesError`): the CLI exits 2, the server starts with an empty book and
prints why, and the card shows it. A file that is not there is a note, not an
error. `tomllib` is 3.11+ and `requires-python` says 3.10, so on 3.10 a rules
file that exists is a load error naming the version.

```toml
[[nudge_rule]]
section = "curve"            # remarks.SECTIONS
knob    = "long_term_vol"    # so key == "curve.long_term_vol"
scope   = { pair = "EURUSD" }
value   = 0.10               # signed, in the knob's own units
spread  = 0.06               # prior half-IQR
weight  = 4                  # pseudo-corrections; 2 <= weight <= 5
why     = "back end lags broker moves; desk marks up before the fit does"
added   = "2026-08-28"

[[plan_rule]]
free_order = ["initial_vol", "long_term_vol", "short_addon", "rate_vol"]
why        = "default order in which curve knobs are freed as targets allow"
```

Two tables because they are two different objects. `nudge_rule` is the
pseudo-correction story above and lands in `learn`; there is one per knob per
pair, and a second is passed over and said. `plan_rule` is discrete -- a
default ordering for `plan_fit`/`choose_knobs` to free by, carried on
`Tendencies.rules` so `plan_fit` reads it off the tendencies it already has and
`consult.confer` needed no new argument. `_free_order` applies it first and the
journal's observed move counts second, each as its own labelled `Choice`; a
knob the curve does not have is passed over with a note. **The hard constraints
are not expressible here**: four targets cannot determine five parameters, and
`informative_params` still governs the wings. A rules file must not be able to
weaken a rule that is true of the model, only to seed a habit.

**Editing is free.** Tendencies are re-derived from the journal at every
`learn`, so the file is a hypothesis and not a commitment: change a value,
re-run, and see what the same journal now says. That is what makes the first
month cheap -- the belief is revisable and the evidence is fixed.

**Where a rule of thumb must not go: `consult.py`'s score.** It counts inside
the observed two-way and nothing else. Weight it by a prior and a belief starts
improving its own score, which reopens exactly the circularity the score was
built to close. Priors live in tendencies; the critique stays a fact about the
archive.

**Where it is switched.** `serve --rules PATH`, `mark --rules PATH`,
`mark --no-rules`, and the **rules of thumb** checkbox on the card (`use_rules`,
posted with the panel and read by `panel_from_request`). `volkit mark rules
PAIR` prints the rules loaded, their weights and any contested flags, then each
rule against the pair's real corrections; `--no-rules` on `mark learn` and
`mark propose` prints the desk-only answer. The two side by side are how anyone
judges whether the priors are helping or talking, and it is the reason the flag
exists.

## What the two agents exchange (`consult.py`)

The quoting agent's most interesting output is a flag it is forbidden to apply
(*the mark is 0.45 below where this has been quoted*); the marking agent's
hardest input is what that flag contains. So they confer, in numbers:

1. **A finding** goes quote-side to mark-side: this instrument at this tenor is
   marked here and has been quoted there, over this many observations from
   this many brokers, this recently.
2. The mark side turns findings into what the existing fitters already take --
   a `CurveTarget` for the at-the-money, a two-way `MarketQuote` built from the
   *observed range* for a wing -- and proposes. Only the at-the-money becomes a
   curve target: a risk reversal is a statement about shape, and feeding one to
   a fit that can only move the level asks a level to explain a skew.
3. **A critique** comes back: with that proposal on the book, how many observed
   markets does the surface sit inside, what improved, and **what it broke**.
4. The mark side weights what it broke by `REWEIGHT` and tries again, at most
   `MAX_ROUNDS` times, and the best round goes to a person.

Two things stop this being circular -- fit to the archive, score against the
archive, of course it improved:

- **The score counts *inside the observed two-way*, not distance to its mid.**
  Anywhere sensible scores the same, so the loop cannot improve its score by
  walking the surface onto the middle of every market it has ever seen. Only
  leaving a market scores worse.
- **Every finding is scored, including the ones no target was built from.**
  The tenors the fit was not aimed at are exactly where a re-mark gets caught
  doing damage.

**No language model is anywhere near this.** Both sides produce numbers; a
model between them could only paraphrase, and `llm.py`'s numeric guard cannot
check a negotiation. What a model may do, at the very end, is describe the
round that won.

A worked consequence worth knowing: with learned pins in force the fit has
fewer free parameters, so its RMSE gets *worse* while matching what the desk
actually does. The critique reports that numerically rather than hiding it,
and adjudicating it is the person's job.

## The two locks, and which order they are taken in

The server holds one book behind `BookService._lock`, and the observation
archive behind `_archive_lock` of its own -- reading a folder can take a
minute, and under the book's lock that is a minute in which no screen answers
(§17). The marking card needs both, and the order is binding:

- **The archive is read first, on its own lock, and that lock is let go before
  the book's is taken.** `Panel.archive_evidence` is the archive half --
  `synthesis.synthesize` and nothing else -- and what it returns is handed to
  `Panel.run` as `archive_evidence`, which then touches no archive at all.
  `webapp.mm_mark` borrows the clock under the book's lock, lets it go, reads
  the archive, and only then takes the book for the numeric work.
- Held the other way round -- the book's lock outstanding while the archive's
  is waited for, which is what `mm_mark` used to do -- **a folder scan or a
  DTCC download froze the fit**. Those hold `_archive_lock` for minutes, with
  a language model behind the scan; the card queued behind them still holding
  the book, and the Fit button beside it, which asks the archive nothing, sat
  spinning until they were done. A test pins it: the book's lock is provably
  not held at the moment the archive's is taken.
- The page cannot tell a queued run from a working one either, so it says:
  the status line counts the seconds after three of them and names the
  possibility after ten (`busy()` in `index.html`, used by the fit, the quote
  and the card).

Files, beside the workbook: `mm_remarks.jsonl` (the journal), and
`mm_rules.toml` (the rules of thumb).
