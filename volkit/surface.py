"""The volatility surface: an ATM curve plus a smile, and everything priced off it.

Replaces the legacy ``Vol``.  The structural change is caching.  The legacy
``get_vol`` rebuilt two SABR calibrations, solved four delta strikes by
fixed-point, and ran a twelve-parameter SVI optimisation *per strike query*.
Here a ``SmileSlice`` is built once per expiry and memoised, so a surface plot
or a delta ladder costs one fit rather than thousands.

The smile parameters themselves are fitted per quoted tenor and then given a
term structure of their own (initial / final / decay), exactly as before, but
with bounded least squares instead of an unconstrained ``minimize`` on a
non-differentiable ``sqrt`` of squared error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np
from scipy.optimize import least_squares

from . import black, sabr
from .atm import AtmCurve
from .banded import BandTreatment
from .black import DeltaConvention
from .numerics import ConvergenceError, fixed_point, solve_scalar
from .sabr import SabrCalibration, SabrParams
from .smile import INTERPOLATORS, SmileSlice
from .timeutil import Clock, tenor_to_years

# The four smile parameters carried across expiries.
PARAM_NAMES = ("slog10", "slog25", "rho25", "rho10")

#: The four quotes one tenor of a pair's sheet holds, in the order a marker
#: reads them: each wing's risk reversal beside its own market strangle.  The
#: workbook's own columns (``marketdata.SMILE_COLUMNS``) and the fields of
#: :class:`SmileMark`; named here so the marking screen, the session file and
#: the workbook writer all mean the same four things by "the quotes".
QUOTE_FIELDS = ("rr_25", "st_25", "rr_10", "st_10")

#: How each is labelled where a person reads it -- the sheet's own headings.
QUOTE_LABELS = {"rr_25": "RR 25d", "st_25": "ST 25d",
                "rr_10": "RR 10d", "st_10": "ST 10d"}

#: The two wings a ratio can derive, and the quote each one produces.  The
#: 10-delta only: a desk marks the 25-delta wing and carries the 10-delta at a
#: multiple of it, never the other way round.
RATIO_WINGS = {"st": "st_10", "rr": "rr_10"}

#: The tab the multipliers live on.  They used to live in the pair sheets'
#: own formulas -- ``ST 10D`` was ``=ST 25D * 3.25`` and ``RR 10D`` was
#: ``=RR 25D * 1.85``, a different multiple per pair and per tenor -- which
#: made the workbook a small spreadsheet model rather than a table of numbers.
#: A tool that writes a quote into such a sheet leaves the wing beside it
#: holding a value computed from the number that has just been replaced, and
#: the next person to open the file in Excel gets a wing that moved on its
#: own.  The multiples are data now, and the wings are derived here.
WING_RATIOS_SHEET = "WING_RATIOS"


@dataclass(frozen=True)
class WingRatio:
    """How one tenor's 10-delta wings follow its 25-delta ones.

    ``None`` is not "one": it is *no ratio*, meaning that wing is quoted in
    its own right at this tenor.  The two are different answers and the
    difference is the whole point of the tab -- a desk that types a 10-delta
    is saying the relationship does not hold there.
    """

    st: float | None = None
    rr: float | None = None

    def get(self, wing: str) -> float | None:
        if wing not in RATIO_WINGS:
            raise ValueError(f"unknown wing {wing!r}; expected one of {', '.join(RATIO_WINGS)}")
        return self.st if wing == "st" else self.rr

    def with_wing(self, wing: str, value: float | None) -> "WingRatio":
        return (replace(self, st=value) if wing == "st" else replace(self, rr=value))


def check_ratio(wing: str, tenor: str, value: float) -> float:
    """One multiplier, refused here rather than at the fit.

    Positive, because a 10-delta wing has the sign of the 25-delta it is
    taken from: a negative multiple turns a put-over smile into a call-over
    one at the wing alone, which is not a ratio anybody means.
    """
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError(f"{wing} wing ratio at {tenor}: a multiple of the 25-delta is "
                         f"positive, got {value!r}")
    return v


def load_wing_ratios(path) -> dict[str, dict[str, WingRatio]]:
    """Read the workbook's ``WING_RATIOS`` tab: pair, tenor, st, rr.

    ``{PAIR: {TENOR: WingRatio}}``.  An absent tab is ``{}`` -- a workbook
    that has not been migrated yet quotes all four wings itself, which is what
    every workbook did before this tab existed.  A blank cell is no ratio for
    that wing at that tenor, which is different from the row being absent only
    in that somebody wrote it down.
    """
    from . import configsheets

    rows = configsheets.read_rows(path, WING_RATIOS_SHEET, required=("pair", "tenor"))
    if rows is None:
        return {}
    out: dict[str, dict[str, WingRatio]] = {}
    for row in rows:
        pair, tenor = row.text("pair").upper(), row.text("tenor").upper()
        if not pair or not tenor:
            continue
        ratio = WingRatio()
        for wing in RATIO_WINGS:
            value = row.real(wing)
            if value is not None:
                ratio = ratio.with_wing(wing, check_ratio(wing, f"{pair} {tenor}", value))
        out.setdefault(pair, {})[tenor] = ratio
    return out

#: The three coefficients of one parameter's term structure, in the order the
#: marking screen shows them.  The same shape as the ATM backbone's initial /
#: long-term / mean-reversion and a cross's correlation curve, so a desk that
#: can read one can read all three.
TERM_COEFFS = ("initial", "final", "decay")


@dataclass
class SmileMark:
    """Broker quotes for one expiry, in decimals."""

    tenor: str
    st_10: float   # 10-delta market strangle
    st_25: float   # 25-delta market strangle
    rr_25: float   # 25-delta risk reversal, call vol minus put vol
    rr_10: float   # 10-delta risk reversal


@dataclass
class TenorFit:
    """The calibration outcome for one quoted tenor."""

    tenor: str
    t: float
    atm_vol: float
    slog10: float
    slog25: float
    rho25: float
    rho10: float
    cal_25: SabrCalibration
    cal_10: SabrCalibration
    ok: bool
    message: str = ""


@dataclass
class ParamTermStructure:
    """``final - (final - initial) * exp(-decay * t)`` for one smile parameter."""

    initial: float
    final: float
    decay: float

    def __call__(self, t):
        t = np.asarray(t, dtype=float)
        return self.final - (self.final - self.initial) * np.exp(-self.decay * t)


def fit_param_term_structure(ts, values, *, name: str = "parameter") -> ParamTermStructure:
    """Bounded least squares for the initial/final/decay shape.

    The legacy ``min_diff`` minimised ``sqrt(sum of squares)``, which is not
    differentiable at its minimum, using an unconstrained quasi-Newton method
    with no bounds -- so a negative decay (an exploding term structure) was a
    perfectly acceptable answer.  Decay is now constrained non-negative and the
    residuals are fed to a least-squares solver in their natural form.
    """
    ts = np.asarray(ts, dtype=float)
    values = np.asarray(values, dtype=float)
    if ts.size == 0:
        raise ValueError(f"no points to fit the {name} term structure")
    if ts.size == 1:
        return ParamTermStructure(float(values[0]), float(values[0]), 1.0)

    def residuals(x):
        return ParamTermStructure(x[0], x[1], x[2])(ts) - values

    lo, hi = float(values[0]), float(values[-1])
    span = max(abs(hi - lo), 1e-6)
    best = None
    for decay0 in (0.5, 2.0, 8.0, 30.0):
        sol = least_squares(
            residuals, np.array([lo, hi, decay0]),
            bounds=(np.array([lo - 10 * span, hi - 10 * span, 0.0]),
                    np.array([lo + 10 * span, hi + 10 * span, 500.0])),
            xtol=1e-14, ftol=1e-14, gtol=1e-14, max_nfev=800,
        )
        if best is None or sol.cost < best.cost:
            best = sol
    return ParamTermStructure(float(best.x[0]), float(best.x[1]), float(best.x[2]))


@dataclass
class VolSurface:
    """ATM curve plus smile, for one currency pair."""

    pair: str
    atm: AtmCurve
    conv: DeltaConvention = field(default_factory=DeltaConvention)
    method: str = "SVI"
    marks: list[SmileMark] = field(default_factory=list)
    fits: list[TenorFit] = field(default_factory=list)
    term: dict[str, ParamTermStructure] = field(default_factory=dict)
    # A term structure the desk has marked, replacing the fitted one for that
    # parameter.  Kept *beside* ``term`` rather than written into it so that a
    # re-fit cannot quietly discard somebody's mark, and so the fitted shape
    # stays visible underneath it as the placeholder on the marking screen --
    # the same arrangement as the ATM curve's tenor overwrites.
    term_marks: dict[str, ParamTermStructure] = field(default_factory=dict)
    anchor_tenors: bool = False
    # Set for managed / pegged pairs.  The lognormal smile below is not a valid
    # model outside a hard band, so queries there are flagged.
    band: object | None = None
    # How much notice to take of that band: flag it, ignore it, or price the
    # regime mixture (method "BAND").  Marked, never inferred -- a wider body
    # and a higher hazard both raise the at-the-money, so a joint fit is
    # degenerate.  See banded.BandTreatment and CLAUDE.md section 6.
    band_treatment: BandTreatment = field(default_factory=BandTreatment)
    # A band is an absolute price range and this surface works in strike over
    # forward, so placing one needs the outright forward at the expiry.  The
    # Book sets this from the spot / forward feed; without it the band model
    # refuses rather than guessing a level.  Signature: (t years) -> forward
    # or None.
    forward_lookup: object | None = None
    param_overwrites: dict[str, dict[str, float]] = field(default_factory=dict)
    # A quote typed on the marking screen, replacing the one the workbook's
    # sheet holds for that tenor -- ``{TENOR: {field: value}}`` in decimals.
    # Kept *beside* ``marks`` and applied at fit time (:meth:`quoted_marks`)
    # for the same reason the parameter overwrites are kept beside the fits:
    # a reload, or the book handing this surface the workbook's marks again,
    # must not quietly discard what somebody typed, and the sheet's own
    # number has to stay visible underneath it as the placeholder.  A tenor
    # the sheet does not quote at all can be created here, and is fitted once
    # all four of its quotes are filled in.
    quote_overwrites: dict[str, dict[str, float]] = field(default_factory=dict)
    # The workbook's ``WING_RATIOS`` tab, for this pair: ``{TENOR: WingRatio}``.
    # Where one is set the 10-delta wing is *derived* from the 25-delta and
    # the sheet's own 10-delta column is not read.
    wing_ratios: dict[str, WingRatio] = field(default_factory=dict)
    # A ratio the screen has changed, in the same layering as the quotes: the
    # tab's number stays underneath as the placeholder, and an explicit
    # ``None`` here is a wing somebody has taken off the ratio and quoted in
    # its own right at that tenor.  ``{TENOR: {"st": v | None, "rr": ...}}``.
    ratio_overwrites: dict[str, dict[str, float | None]] = field(default_factory=dict)
    # Additive adjustments to the four smile parameters, applied across the
    # whole curve.  An overwrite *replaces* a parameter and so flattens its
    # term structure; a shift moves the level and keeps the shape, which is
    # what re-marking a wing against a broker run actually means.  The
    # market-maker screen tunes these; zero is the marked surface.
    param_shifts: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.conv, DeltaConvention):
            self.conv = DeltaConvention(bool(self.conv))
        self._slices: dict[tuple, SmileSlice] = {}

    @property
    def clock(self) -> Clock:
        return self.atm.clock

    def tenor_years(self, tenor: str) -> float:
        """Years to a quoted tenor's calendar expiry, through the ATM curve.

        A mark is quoted against a tenor and priced against a date, and this
        is where the two meet.  It delegates rather than computing, so a
        surface and its own ATM curve can never place ``3M`` differently.
        """
        return self.atm.tenor_years(tenor)

    # -- the quotes, as marked --------------------------------------------
    def effective_ratio(self, tenor: str) -> WingRatio:
        """The multipliers in force at one tenor: the screen's over the tab's."""
        key = str(tenor).upper()
        ratio = self.wing_ratios.get(key, WingRatio())
        for wing, value in self.ratio_overwrites.get(key, {}).items():
            ratio = ratio.with_wing(wing, value)
        return ratio

    def overwrite_ratio(self, tenor: str, wing: str, value: float | None) -> None:
        """Set, or take off, one wing's multiplier at one tenor.

        ``None`` takes the wing off the ratio: it is then quoted in its own
        right and the box beside it on the screen is what is used.
        """
        if wing not in RATIO_WINGS:
            raise ValueError(f"unknown wing {wing!r}; expected one of {', '.join(RATIO_WINGS)}")
        key = str(tenor).upper()
        self.ratio_overwrites.setdefault(key, {})[wing] = (
            None if value is None else check_ratio(wing, key, value))

    def clear_ratio_overwrite(self, tenor: str | None = None,
                              wing: str | None = None) -> None:
        """Give a multiplier, a tenor, or every change back to the tab."""
        if tenor is None:
            self.ratio_overwrites.clear()
            return
        key = str(tenor).upper()
        if wing is None:
            self.ratio_overwrites.pop(key, None)
            return
        edits = self.ratio_overwrites.get(key)
        if edits is not None:
            edits.pop(wing, None)
            if not edits:
                self.ratio_overwrites.pop(key, None)

    def ratio_rows(self) -> list[dict]:
        """One row per tenor the ratios or the quotes reach: tab, screen, both."""
        tenors = {m.tenor.upper() for m in self.marks} | set(self.wing_ratios) \
            | set(self.quote_overwrites) | set(self.ratio_overwrites)
        rows = []
        for tenor in tenors:
            sheet = self.wing_ratios.get(tenor, WingRatio())
            live = self.effective_ratio(tenor)
            row = {"tenor": tenor, "marked": False}
            for wing in RATIO_WINGS:
                row[wing] = live.get(wing)
                row[wing + "_sheet"] = sheet.get(wing)
                if wing in self.ratio_overwrites.get(tenor, {}) and \
                        live.get(wing) != sheet.get(wing):
                    row["marked"] = True
            rows.append(row)
        return sorted(rows, key=lambda r: tenor_to_years(r["tenor"]))

    def quoted_marks(self) -> list[SmileMark]:
        """The sheet's quotes with the screen's edits on them.

        What :meth:`fit_smiles` actually fits.  ``marks`` stays exactly as the
        workbook gave it and every edit lives in ``quote_overwrites``, so
        clearing a box gives that quote back to the sheet without a reload and
        the sheet's number can be shown underneath the typed one.

        A tenor that exists only as an edit is a tenor the sheet does not
        quote yet.  It is fitted once all four of its quotes are there and
        reported until then, because half a smile is not a smile and a tenor
        that quietly did not fit is the silent absence this project refuses.
        """
        by_tenor = {m.tenor.upper(): m for m in self.marks}
        out: list[SmileMark] = []
        for mark in self.marks:
            edits = self.quote_overwrites.get(mark.tenor.upper())
            out.append(self._derive(replace(mark, **edits) if edits else mark))
        for tenor, edits in self.quote_overwrites.items():
            if tenor in by_tenor:
                continue
            # A wing a ratio derives does not have to be typed: with a ratio
            # row a new tenor is two numbers, not four.
            ratio = self.effective_ratio(tenor)
            derived = {RATIO_WINGS[w] for w in RATIO_WINGS if ratio.get(w) is not None}
            missing = [f for f in QUOTE_FIELDS if f not in edits and f not in derived]
            if missing:
                self.warnings.append(
                    f"{self.pair} {tenor}: the sheet does not quote this tenor and "
                    f"{', '.join(QUOTE_LABELS[f] for f in missing)} "
                    f"{'is' if len(missing) == 1 else 'are'} still blank, so it was not "
                    f"fitted; a tenor is quoted by all four of its numbers or by none")
                continue
            values = {f: edits.get(f, 0.0) for f in QUOTE_FIELDS}
            out.append(self._derive(SmileMark(tenor=tenor, **values)))
        return sorted(out, key=lambda m: tenor_to_years(m.tenor))

    def _derive(self, mark: SmileMark) -> SmileMark:
        """The 10-delta wings a ratio is set for, taken from the 25-delta.

        Applied *after* the typed quotes and not before, because a ratio is
        the last word on a wing it governs: the alternative is a screen where
        the 10-delta box and the ratio beside it both claim the same number
        and the fit quietly picks one.  Typing a 10-delta takes that wing off
        its ratio (:meth:`overwrite_quote`), which is how the box wins.
        """
        ratio = self.effective_ratio(mark.tenor)
        changes = {}
        for wing, target in RATIO_WINGS.items():
            factor = ratio.get(wing)
            if factor is not None:
                changes[target] = getattr(mark, target.replace("_10", "_25")) * factor
        return replace(mark, **changes) if changes else mark

    def overwrite_quote(self, tenor: str, field_name: str, value: float) -> None:
        """Type one quote over the sheet's, for one tenor.

        The same two checks the workbook reader makes on the cell this
        replaces (``marketdata._load_marks``): a number, and a strangle above
        zero.  Refused here rather than at the fit, where it would come back
        as a convergence failure that names neither the tenor nor the box.

        Typing a **10-delta** wing that a ratio governs takes it off that
        ratio at this tenor, because otherwise the two would both claim the
        wing and the ratio, applied last, would win over the box that was just
        typed into -- a number that goes back to what it was the moment you
        leave the field.  Clearing the box puts the ratio back.
        """
        if field_name not in QUOTE_FIELDS:
            raise ValueError(f"unknown quote {field_name!r}; expected one of "
                             f"{', '.join(QUOTE_FIELDS)}")
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{QUOTE_LABELS[field_name]} at {tenor}: {value!r} is not a number")
        if field_name.startswith("st_") and v <= 0:
            raise ValueError(f"{QUOTE_LABELS[field_name]} at {tenor}: a market strangle is "
                             f"positive, got {v * 100:.4g}")
        try:
            tenor_to_years(tenor)
        except Exception as exc:  # noqa: BLE001 - the tenor is the thing being reported
            raise ValueError(f"{tenor!r} is not a tenor this book can place ({exc})") from None
        key = str(tenor).upper()
        self.quote_overwrites.setdefault(key, {})[field_name] = v
        wing = next((w for w, f in RATIO_WINGS.items() if f == field_name), None)
        if wing is not None and self.effective_ratio(key).get(wing) is not None:
            self.overwrite_ratio(key, wing, None)

    def clear_quote_overwrite(self, tenor: str | None = None,
                              field_name: str | None = None) -> None:
        """Give a quote, a tenor, or every edit back to the sheet.

        A 10-delta wing taken off its ratio by being typed into goes back onto
        it here: the two moves are one decision and they are undone together.
        """
        if tenor is None:
            self.quote_overwrites.clear()
            for tenor_key in list(self.ratio_overwrites):
                for wing in list(self.ratio_overwrites[tenor_key]):
                    if self.ratio_overwrites[tenor_key][wing] is None:
                        self.clear_ratio_overwrite(tenor_key, wing)
            return
        key = str(tenor).upper()
        wings = ([w for w, f in RATIO_WINGS.items() if f == field_name]
                 if field_name is not None else list(RATIO_WINGS))
        for wing in wings:
            if self.ratio_overwrites.get(key, {}).get(wing, "keep") is None:
                self.clear_ratio_overwrite(key, wing)
        if field_name is None:
            self.quote_overwrites.pop(key, None)
            return
        edits = self.quote_overwrites.get(key)
        if edits is not None:
            edits.pop(field_name, None)
            if not edits:
                self.quote_overwrites.pop(key, None)

    def quote_rows(self) -> list[dict]:
        """One row per tenor: the sheet's quotes, the typed ones, and both.

        Every tenor the sheet quotes and every tenor somebody has typed into,
        so a tenor that exists only as an edit is on the screen that created
        it.  ``marked`` is *different from the sheet*, not merely typed: a
        value typed back onto the number that was already there is not a mark
        and must not carry a dot that says the row was changed.
        """
        sheet = {m.tenor.upper(): m for m in self.marks}
        fitted = {f.tenor.upper() for f in self.fits}
        # What the fit is actually using, derivation included, so the box a
        # wing is read from shows the number the smile was built on.
        live = {m.tenor.upper(): m for m in self.quoted_marks()}
        names = list(sheet) + [t for t in self.quote_overwrites if t not in sheet]
        rows = []
        for tenor in names:
            base, edits = sheet.get(tenor), self.quote_overwrites.get(tenor, {})
            ratio = self.effective_ratio(tenor)
            row = {"tenor": tenor, "quoted": base is not None, "fitted": tenor in fitted,
                   "marked": False}
            for f in QUOTE_FIELDS:
                b = getattr(base, f) if base is not None else None
                o = edits.get(f)
                wing = next((w for w, name in RATIO_WINGS.items() if name == f), None)
                factor = ratio.get(wing) if wing else None
                on_ratio = factor is not None
                if on_ratio and tenor in live:
                    value = getattr(live[tenor], f)
                elif on_ratio:
                    value = None
                else:
                    value = o if o is not None else b
                row[f] = value
                row[f + "_sheet"] = b
                # A derived wing is not typed and not the sheet's either: the
                # screen shows it read-only with the multiple that made it.
                row[f + "_derived"] = factor if on_ratio else None
                if not on_ratio and o is not None and (b is None or abs(o - b) > 1e-12):
                    row["marked"] = True
            rows.append(row)
        return sorted(rows, key=lambda r: tenor_to_years(r["tenor"]))

    # -- calibration ------------------------------------------------------
    def fit_smiles(self, marks: list[SmileMark] | None = None,
                   only: list[str] | None = None, *,
                   max_solutions: int = 1, prior_weight: float = 0.0) -> list[TenorFit]:
        """Calibrate SABR at each quoted tenor for both the 25d and 10d wings.

        Tenors are fitted short to long.  With ``prior_weight`` above zero each
        tenor is pulled toward the one before it, which stabilises the
        parameter term structure when the quotes admit more than one fit.
        ``max_solutions`` above 1 makes each fit report competing solutions.
        """
        if marks is not None:
            self.marks = list(marks)
        wanted = {t.upper() for t in only} if only else None
        keep = [f for f in self.fits if wanted and f.tenor.upper() not in wanted]
        results: list[TenorFit] = []
        prev25 = prev10 = None
        # The sheet's quotes with the marking screen's edits on them, which is
        # the only thing that is ever fitted; ``marks`` stays the workbook's.
        for mark in self.quoted_marks():
            if wanted and mark.tenor.upper() not in wanted:
                continue
            t = self.tenor_years(mark.tenor)
            atm_vol = self.atm.cut_vol(self.clock.datetime_from_years(t), "NY")
            if atm_vol <= 0:
                atm_vol = self.atm.term_vol(t)
            msgs = []
            try:
                c25 = sabr.calibrate(atm_vol, mark.rr_25, mark.st_25, 0.25, t, self.conv,
                                     prior=prev25, prior_weight=prior_weight,
                                     max_solutions=max_solutions)
                c10 = sabr.calibrate(atm_vol, mark.rr_10, mark.st_10, 0.10, t, self.conv,
                                     prior=prev10, prior_weight=prior_weight,
                                     max_solutions=max_solutions)
                prev25, prev10 = c25.params, c10.params
                for cal, wing in ((c25, "25d"), (c10, "10d")):
                    msgs.extend(f"{wing}: {w}" for w in cal.warnings if "no (rho" not in w)
                ok = c25.converged and c10.converged
                if not c25.converged:
                    msgs.append(f"25d: {c25.message}")
                if not c10.converged:
                    msgs.append(f"10d: {c10.message}")
                results.append(TenorFit(
                    tenor=mark.tenor, t=t, atm_vol=atm_vol,
                    slog10=c10.params.log_volvol, slog25=c25.params.log_volvol,
                    rho25=c25.params.rho, rho10=c10.params.rho,
                    cal_25=c25, cal_10=c10, ok=ok, message="; ".join(msgs),
                ))
            except (ConvergenceError, ValueError) as exc:
                self.warnings.append(f"{self.pair} {mark.tenor}: smile calibration failed ({exc})")
        self.fits = sorted(keep + results, key=lambda f: f.t)
        self._slices.clear()
        return self.fits

    def interpolate_params(self) -> dict[str, ParamTermStructure]:
        """Give each smile parameter a term structure across expiries."""
        if not self.fits:
            raise ValueError(f"{self.pair}: fit_smiles must run before interpolate_params")
        ts = [f.t for f in self.fits]
        self.term = {
            name: fit_param_term_structure(ts, [getattr(f, name) for f in self.fits], name=name)
            for name in PARAM_NAMES
        }
        self._slices.clear()
        return self.term

    def calibrate(self, marks: list[SmileMark] | None = None) -> "VolSurface":
        """Fit every tenor then build the parameter term structures."""
        self.fit_smiles(marks)
        if self.fits:
            self.interpolate_params()
        return self

    # -- parameters at an arbitrary expiry --------------------------------
    def param_curve(self, name: str) -> ParamTermStructure:
        """The term structure in force for one smile parameter.

        The marked one if there is one, the fitted one otherwise.  Every
        reader of a parameter's shape goes through here, so a marked curve
        cannot reach one caller and miss another.
        """
        if name not in PARAM_NAMES:
            raise ValueError(f"unknown smile parameter {name!r}; expected one of {PARAM_NAMES}")
        marked = self.term_marks.get(name)
        if marked is not None:
            return marked
        if name not in self.term:
            raise ValueError(f"{self.pair}: no smile term structure; run calibrate() first")
        return self.term[name]

    def params_at(self, t: float) -> dict[str, float]:
        """The four smile parameters at ``t``.

        With ``anchor_tenors`` on, the curve is pinned to the fitted values at
        the quoted tenors and the shape between them comes from the term
        structure -- the legacy ``use_overwrite`` behaviour, but without the
        division by ``v2_c - v1_c`` that blew up whenever the term structure
        happened to be flat between two tenors.
        """
        if not self.term and not self.term_marks:
            raise ValueError(f"{self.pair}: no smile term structure; run calibrate() first")
        curves = {name: self.param_curve(name) for name in PARAM_NAMES}
        out = {name: float(curves[name](t)) for name in PARAM_NAMES}
        for name, ow in self.param_overwrites.items():
            if name in out and "curve" in ow:
                out[name] = float(ow["curve"])
        if not self.anchor_tenors or not self.fits:
            return self._shifted(out)

        ts = [f.t for f in self.fits]
        if t <= ts[0] or t >= ts[-1]:
            idx = 0 if t <= ts[0] else len(ts) - 1
            for name in PARAM_NAMES:
                out[name] = self._anchor_value(name, idx)
            return out
        j = int(np.searchsorted(np.array(ts), t, side="left"))
        i = j - 1
        t1, t2 = ts[i], ts[j]
        for name in PARAM_NAMES:
            v1, v2 = self._anchor_value(name, i), self._anchor_value(name, j)
            c1, c2 = float(curves[name](t1)), float(curves[name](t2))
            ct = float(curves[name](t))
            denom = c2 - c1
            # Fall back to linear in time when the model curve is flat across
            # the interval, instead of dividing by (almost) zero.
            ratio = (ct - c1) / denom if abs(denom) > 1e-12 else (t - t1) / (t2 - t1)
            out[name] = v1 + ratio * (v2 - v1)
        return self._shifted(out)

    def _shifted(self, out: dict[str, float]) -> dict[str, float]:
        """Apply ``param_shifts`` to a parameter set.

        The correlations are clamped just inside their domain because SABR is
        undefined at |rho| = 1, not to hide anything: ``shift_warnings`` reports
        every clamp, and the market-maker fit bounds the shifts so it cannot ask
        for one in the first place.
        """
        if not self.param_shifts:
            return out
        for name, delta in self.param_shifts.items():
            if name not in out or not delta:
                continue
            value = out[name] + float(delta)
            if name.startswith("rho"):
                value = min(max(value, -0.999), 0.999)
            elif name.startswith("slog"):
                value = max(value, 1e-6)
            out[name] = value
        return out

    def shift_warnings(self) -> list[str]:
        """Flag shifts that the clamp in ``_shifted`` is silently absorbing."""
        out = []
        if not self.param_shifts or (not self.term and not self.term_marks):
            return out
        ts = [f.t for f in self.fits] or [0.25]
        for name, delta in self.param_shifts.items():
            if name not in PARAM_NAMES or not delta:
                continue
            curve = self.param_curve(name)
            raw = [float(curve(t)) + float(delta) for t in ts]
            if name.startswith("rho") and any(abs(v) > 0.999 for v in raw):
                out.append(
                    f"{self.pair}: the {name} shift of {delta:+.4f} pushes rho past "
                    f"{max(raw, key=abs):+.4f} at one of the quoted tenors and is being clamped "
                    f"to +/-0.999; the wing cannot get any steeper at this beta")
            if name.startswith("slog") and any(v <= 1e-6 for v in raw):
                out.append(
                    f"{self.pair}: the {name} shift of {delta:+.4f} takes the volatility of "
                    f"volatility to zero or below at one of the quoted tenors and is being "
                    f"clamped; the smile is flat there")
        return out

    def set_param_shifts(self, shifts: dict[str, float]) -> list[str]:
        """Replace the whole shift set.  Returns problems; a bad set is rejected."""
        problems = [f"unknown smile parameter {k!r}; expected one of {PARAM_NAMES}"
                    for k in shifts if k not in PARAM_NAMES]
        problems += [f"the {k} shift must be a finite number, got {v!r}"
                     for k, v in shifts.items() if not isinstance(v, (int, float))
                     or not math.isfinite(float(v))]
        if problems:
            return problems
        self.param_shifts = {k: float(v) for k, v in shifts.items() if v}
        self._slices.clear()
        return []

    def clear_param_shifts(self) -> None:
        self.param_shifts.clear()
        self._slices.clear()

    def _anchor_value(self, name: str, index: int) -> float:
        fit = self.fits[index]
        ow = self.param_overwrites.get(name, {})
        return float(ow.get(fit.tenor.upper(), getattr(fit, name)))

    def overwrite_param(self, name: str, tenor: str, value: float) -> None:
        """Pin one smile parameter, at a tenor or on the whole curve."""
        if name not in PARAM_NAMES:
            raise ValueError(f"unknown smile parameter {name!r}; expected one of {PARAM_NAMES}")
        self.param_overwrites.setdefault(name, {})[tenor.upper()] = float(value)
        self._slices.clear()

    def clear_param_overwrites(self) -> None:
        self.param_overwrites.clear()
        self._slices.clear()

    # -- the parameter term structures, marked ----------------------------
    def set_param_term(self, name: str, initial: float, final: float,
                       decay: float) -> list[str]:
        """Mark one smile parameter's whole term structure.

        The three coefficients of ``final - (final - initial) * exp(-decay t)``,
        in the raw units the parameter carries: a correlation strictly inside
        (-1, 1), a volatility of volatility above zero, and a decay that is not
        negative -- the same bound ``fit_param_term_structure`` puts on its
        solver, and for the same reason, because a negative decay is a term
        structure that explodes rather than settling.  Returns problems; a bad
        set is rejected whole, so half a curve is never marked.
        """
        if name not in PARAM_NAMES:
            return [f"unknown smile parameter {name!r}; expected one of {PARAM_NAMES}"]
        vals = {"initial": initial, "final": final, "decay": decay}
        problems = [f"the {name} {coeff} must be a finite number, got {v!r}"
                    for coeff, v in vals.items()
                    if not isinstance(v, (int, float)) or isinstance(v, bool)
                    or not math.isfinite(float(v))]
        if problems:
            return problems
        for coeff in ("initial", "final"):
            v = float(vals[coeff])
            if name.startswith("rho") and not -1.0 < v < 1.0:
                problems.append(f"the {name} {coeff} is a correlation and must lie strictly "
                                f"inside (-1, 1), got {v:g}; SABR is undefined at |rho| = 1")
            if name.startswith("slog") and v <= 0.0:
                problems.append(f"the {name} {coeff} is a volatility of volatility and must "
                                f"be positive, got {v:g}")
        if float(vals["decay"]) < 0.0:
            problems.append(f"the {name} decay must not be negative, got {vals['decay']:g}; "
                            f"a negative decay is a term structure that runs away with tenor "
                            f"rather than settling")
        if problems:
            return problems
        self.term_marks[name] = ParamTermStructure(float(initial), float(final), float(decay))
        self._slices.clear()
        return []

    def clear_param_terms(self, name: str | None = None) -> None:
        """Give one parameter, or all four, its fitted term structure back.

        A name that is not a smile parameter is refused rather than quietly
        clearing nothing: a clear that reports success and leaves the mark
        standing is the failure this project exists to remove.
        """
        if name is None:
            self.term_marks.clear()
        else:
            if name not in PARAM_NAMES:
                raise ValueError(
                    f"unknown smile parameter {name!r}; expected one of {PARAM_NAMES}")
            self.term_marks.pop(name, None)
        self._slices.clear()

    def term_rows(self) -> list[dict]:
        """Each parameter's term structure, fitted and as marked.

        The fitted coefficients travel beside the marked ones so the screen
        can show what the fit said underneath what somebody typed over it,
        rather than losing it the moment the first coefficient is marked.
        """
        rows = []
        for name in PARAM_NAMES:
            fitted = self.term.get(name)
            marked = self.term_marks.get(name)
            rows.append({
                "param": name,
                "fitted": None if fitted is None else
                {c: getattr(fitted, c) for c in TERM_COEFFS},
                "marked": None if marked is None else
                {c: getattr(marked, c) for c in TERM_COEFFS},
            })
        return rows

    # -- slices -----------------------------------------------------------
    def slice_at(self, expiry, method: str | None = None, cut: str = "TK",
                 forward: float = 1.0) -> SmileSlice:
        """Build (or fetch) the cached smile for an expiry."""
        method = method or self.method
        if method not in INTERPOLATORS:
            raise ValueError(f"unknown interpolation method {method!r}; expected one of {INTERPOLATORS}")
        dt = self.clock.coerce_datetime(expiry)
        t = self.clock.years_to(dt)
        if t <= 0:
            raise ValueError(f"expiry {dt:%Y-%m-%d %H:%M} is not in the future")
        # The treatment is part of the key, not just a field: two slices for
        # the same expiry under different hazards are different smiles, and a
        # cache that could not tell them apart would serve the first answer
        # for the rest of the session.
        key = (round(t, 10), method, cut.upper(), round(forward, 10),
               self.band_treatment if method == "BAND" else None,
               self._band_placement(t) if method == "BAND" else None)
        hit = self._slices.get(key)
        if hit is not None:
            return hit

        atm_vol = self.atm.cut_vol(dt, cut)
        if atm_vol <= 0:
            raise ValueError(f"{self.pair}: ATM volatility is zero at {dt:%Y-%m-%d}")
        p = self.params_at(t)
        sqt = math.sqrt(t)
        s25 = SabrParams(
            alpha=sabr.alpha_from_atm(atm_vol, black.dns_strike(forward, atm_vol, t, self.conv),
                                      p["rho25"], p["slog25"] / sqt, t, forward),
            rho=p["rho25"], volvol=p["slog25"] / sqt, t=t, f=forward)
        s10 = SabrParams(
            alpha=sabr.alpha_from_atm(atm_vol, black.dns_strike(forward, atm_vol, t, self.conv),
                                      p["rho10"], p["slog10"] / sqt, t, forward),
            rho=p["rho10"], volvol=p["slog10"] / sqt, t=t, f=forward)
        band = self.band_for_slice(t, forward) if method == "BAND" else None
        sl = SmileSlice.build(t, atm_vol, s25, s10, self.conv, forward=forward, method=method,
                              band=band, treatment=self.band_treatment)
        self._slices[key] = sl
        return sl

    # -- managed bands ----------------------------------------------------
    def _band_placement(self, t: float) -> float | None:
        """The absolute forward a band slice would be placed against.

        Part of the cache key for the same reason the treatment is: the feed
        is a publication and is re-read all morning (see the auto-reload
        switch), the peg is placed against whatever it then says, and two
        spots are two smiles.  Without this the band card printed the
        republished forward in its own column beside probabilities still
        calibrated against the old one -- the cached slice was served because
        nothing in its key had changed.

        It is read for every band slice rather than only for the moneyness
        ones ``band_for_slice`` actually looks the feed up for: a key that has
        to reproduce a decision made further down is a second place for that
        decision to live.  The cost of the difference is one recompute of an
        absolute-forward slice when the feed moves.  A lookup that fails is
        not the cache's business to report -- ``band_for_slice`` raises the
        real message with the real diagnosis a moment later.
        """
        if self.band is None or self.forward_lookup is None:
            return None
        try:
            level = self.forward_lookup(t)
        except Exception:  # noqa: BLE001 - band_for_slice reports this properly
            return None
        return round(float(level), 10) if level else None

    def band_for_slice(self, t: float, forward: float):
        """This pair's band, moved into the space a slice at ``forward`` uses.

        The outstanding piece of plumbing for the band model was exactly this:
        the surface works in strike over forward while a band is an absolute
        price range.  A slice built at the outright forward already lives in
        the band's own space; one built in moneyness (forward 1.0, which is
        every query that does not name a forward) needs the outright to divide
        by, and that comes from the feed.  Without one there is no honest way
        to place 7.75-7.85 against a moneyness of 1.02, so this refuses and
        says what to load.
        """
        if self.band is None:
            raise ValueError(
                f"{self.pair} has no managed band, so there is no barrier to treat; "
                f"the BAND method applies to pegged pairs listed on the PEG_BANDS tab")
        band = self.band_treatment.effective_band(self.band)
        if band.contains(forward):
            return band
        absolute = None
        if self.forward_lookup is not None:
            absolute = self.forward_lookup(t)
        if not absolute:
            raise ValueError(
                f"{self.pair}: the {band.pair} band is the absolute range "
                f"[{band.lower:g}, {band.upper:g}] and this smile is being read in moneyness "
                f"against a forward of {forward:g}. Load a spot / forward feed for {self.pair} "
                f"(volkit serve --feed ...) so the band can be placed, or price at an outright "
                f"forward instead")
        absolute = float(absolute)
        if not band.contains(absolute):
            raise ValueError(
                f"{self.pair}: the {t:.4f}-year forward of {absolute:.5f} is outside the band "
                f"[{band.lower:g}, {band.upper:g}]. Either the peg has moved and PEG_BANDS is "
                f"stale, or the feed is wrong; the band model cannot be calibrated to a forward "
                f"it does not contain")
        return self.band_treatment.scaled(self.band, forward / absolute)

    def set_band_treatment(self, treatment: BandTreatment) -> list[str]:
        """Re-mark how the band is treated.  Returns what it wants said."""
        if not isinstance(treatment, BandTreatment):
            raise TypeError(f"expected a BandTreatment, got {type(treatment).__name__}")
        self.band_treatment = treatment
        self._slices.clear()
        out = list(treatment.warnings())
        if self.band is None and treatment.mode != "warn":
            out.append(f"{self.pair} has no band on the PEG_BANDS tab, so this treatment does nothing")
        return out

    # -- the pricing surface ----------------------------------------------
    def vol(self, strike_ratio, expiry, method: str | None = None, cut: str = "TK"):
        """Implied volatility for a strike/forward ratio.  Vectorised over strikes."""
        return self.slice_at(expiry, method, cut).vol(strike_ratio)

    def band_check(self, strike_abs, forward_abs: float, method: str | None = None) -> list[str]:
        """Warn about strikes a lognormal smile has no business pricing.

        For a pegged pair the terminal distribution is a regime mixture, so an
        option struck outside the band is worth only whatever the peg breaking
        is worth -- not what a lognormal wing says it is.

        The treatment decides whether this is said at all: ``off`` is a
        deliberate marking that this range is not defended, and a strike
        priced with the ``BAND`` method already has the peg in it.  Everything
        else is flagged.
        """
        if self.band is None or self.band_treatment.mode == "off":
            return []
        if (method or self.method) == "BAND":
            return []
        band = self.band_treatment.effective_band(self.band)
        ks = np.atleast_1d(np.asarray(strike_abs, dtype=float))
        bad = ks[(ks < band.lower) | (ks > band.upper)]
        if bad.size == 0:
            return []
        fix = ("Price it with the BAND interpolation method for a defensible value."
               if self.band_treatment.mode == "warn" else
               "The band treatment is set to the regime mixture, but this price was made "
               "with the lognormal smile; switch the method to BAND.")
        return [
            f"{self.pair} strike {float(bad[0]):.5f} lies outside the managed band "
            f"[{band.lower:g}, {band.upper:g}]; the lognormal smile prices it as if "
            f"the peg did not exist. " + fix
        ]

    def atm_vol(self, expiry, cut: str = "TK") -> float:
        return self.atm.cut_vol(expiry, cut)

    def daily_vol(self, when) -> float:
        return self.atm.daily_vol(when)

    def delta_strike(self, expiry, delta: float, is_call: bool,
                     method: str | None = None, cut: str = "TK") -> tuple[float, float]:
        """Strike and volatility for a delta, solved on the interpolated smile."""
        signed = abs(delta) if is_call else -abs(delta)
        return self.slice_at(expiry, method, cut).strike_from_delta(signed, is_call)

    def risk_reversal(self, expiry, delta: float, method: str | None = None,
                      cut: str = "TK") -> float:
        """Smile risk reversal: call vol minus put vol at ``delta``."""
        _, cv = self.delta_strike(expiry, delta, True, method, cut)
        _, pv = self.delta_strike(expiry, delta, False, method, cut)
        return cv - pv

    def strangle(self, expiry, delta: float, method: str | None = None,
                 cut: str = "TK") -> float:
        """Market strangle implied by the surface, in vol over ATM.

        Solved by bracketing on the strangle premium, replacing the legacy
        ``fsolve(func, 0)`` which reported no diagnostics.
        """
        sl = self.slice_at(expiry, method, cut)
        t, f, atm = sl.t, sl.forward, sl.atm_vol

        def premium_gap(s: float) -> float:
            v = atm + s
            if v <= 0:
                return 1e6
            kc = black.strike_from_delta(abs(delta), f, v, t, True, self.conv)
            kp = black.strike_from_delta(-abs(delta), f, v, t, False, self.conv)
            market = float(black.price(f, kc, v, t, True) + black.price(f, kp, v, t, False))
            model = float(black.price(f, kc, float(sl.vol(kc)), t, True)
                          + black.price(f, kp, float(sl.vol(kp)), t, False))
            return model - market

        return solve_scalar(premium_gap, 0.0, lo_bound=-atm * 0.9,
                            bracket=(-atm * 0.5, atm * 2.0), what="market strangle")

    # -- greeks -----------------------------------------------------------
    def smile_delta(self, fwd: float, strike: float, expiry, is_call: bool = True,
                    method: str | None = None, cut: str = "TK", bump: float = 1e-3,
                    *, conv: DeltaConvention | bool | None = None) -> float:
        """Delta including the smile's reaction to the forward, centrally.

        The smile is a function of ``strike / forward``, so moving the forward
        under a fixed strike moves the volatility that strike is marked at:
        this is ``black.delta + vega * dsigma/dF``, and a test pins it against
        exactly that.

        The level bumped is the **forward** -- it is what Black-76 is priced
        off and what the smile's own ratio is taken against.  The argument was
        called ``spot`` and the pricing screen took that at its word and
        handed it spot, which on any pair with forward points is a different
        option from the one the row above was priced on: a 3M EURUSD ATM came
        back at a 44 delta.  Every other caller was already passing a forward.

        ``conv`` overrides the surface's own delta convention.  It exists for
        one caller: ``analytics.carry_table`` values a **term currency** P&L,
        and the premium-adjusted delta is a hedge ratio in the *other*
        currency rather than ``dV/dF``, so multiplying it by a move in the
        forward does not give the money the position made.  That table asks
        for ``conv=False`` and says so; everything else wants the surface's
        own convention, which is what a desk quotes and hedges in.
        """
        use = self.conv if conv is None else conv
        sl = self.slice_at(expiry, method, cut)
        up, dn = fwd * (1.0 + bump), fwd * (1.0 - bump)
        pv_up = float(black.price(up, strike, float(sl.vol(strike / up)), sl.t, is_call,
                                  foreign_premium=bool(use)))
        pv_dn = float(black.price(dn, strike, float(sl.vol(strike / dn)), sl.t, is_call,
                                  foreign_premium=bool(use)))
        d_fwd = (up - dn) if not bool(use) else (up - dn) / fwd
        return (pv_up - pv_dn) / d_fwd

    def smile_gamma(self, fwd: float, strike: float, expiry, is_call: bool = True,
                    method: str | None = None, cut: str = "TK", bump: float = 0.01,
                    *, conv: DeltaConvention | bool | None = None) -> float:
        """Change in the smile delta for a ``bump`` relative move in the forward.

        The delta this differences is :meth:`smile_delta`, so the smile moves
        with spot here too: what comes back is the whole gamma a desk carries,
        ``dDelta/dS`` along the smile rather than at a frozen volatility, and
        it inherits the surface's delta convention rather than choosing a
        second one.

        The bump is a *relative* move and defaults to the one per cent a desk
        quotes its gamma over, so the number is the delta difference per one
        such move, taken centrally.  Multiplied by a notional it is the cash
        gamma: how much base the hedge has to be moved by when spot moves one
        per cent.  A finite move is the point rather than a limitation -- the
        one per cent is what is being asked about, and taking it as a limit
        would answer a slightly different question about a curve that is not
        quadratic over that distance.
        """
        if not bump > 0:
            raise ValueError(f"smile gamma bump must be positive, got {bump!r}")
        up = self.smile_delta(fwd * (1.0 + bump), strike, expiry, is_call,
                              method, cut, conv=conv)
        dn = self.smile_delta(fwd * (1.0 - bump), strike, expiry, is_call,
                              method, cut, conv=conv)
        return (up - dn) / 2.0

    def density(self, strike_ratio: float, expiry, method: str | None = None,
                cut: str = "TK", bump: float = 1e-3) -> float:
        """Risk-neutral density, as the second strike derivative of a call.

        The legacy ``getDensity`` raised ``NameError`` on an undefined ``S``
        whenever ``delta_adjust`` was false, and divided by ``step**2`` rather
        than ``(K * step)**2``, so the scale was wrong by a factor of ``K^2``
        even on the branch that ran.
        """
        sl = self.slice_at(expiry, method, cut)
        K = float(strike_ratio)
        h = K * bump
        ks = np.array([K - h, K, K + h])
        vols = np.asarray(sl.vol(ks), dtype=float)
        pv = np.array([float(black.price(sl.forward, k, v, sl.t, True)) for k, v in zip(ks, vols)])
        return float((pv[0] - 2.0 * pv[1] + pv[2]) / (h * h))

    def digital(self, spot: float, strike: float, expiry, ramp: float = 0.5,
                is_call: bool = True, method: str | None = None, cut: str = "TK") -> float:
        """Call/put spread replication of a digital, priced on the smile."""
        sl = self.slice_at(expiry, method, cut)
        k2 = strike * (1.0 + ramp / 100.0) if is_call else strike * (1.0 - ramp / 100.0)
        width = abs(k2 - strike)
        if width <= 0:
            raise ValueError(f"digital ramp must be non-zero, got {ramp!r}")
        v1 = float(sl.vol(strike / spot))
        v2 = float(sl.vol(k2 / spot))
        p1 = float(black.price(spot, strike, v1, sl.t, is_call, foreign_premium=True))
        p2 = float(black.price(spot, k2, v2, sl.t, is_call, foreign_premium=True))
        return (p1 * (k2 / width)) - (p2 * (strike / width))

    def smile_table(self, expiry, deltas=(0.10, 0.25), method: str | None = None,
                    cut: str = "TK") -> list[dict]:
        """The quoted smile points for display."""
        sl = self.slice_at(expiry, method, cut)
        rows = []
        for d in sorted(deltas, reverse=True):
            kp, vp = sl.strike_from_delta(-d, False)
            rows.append({"label": f"{int(d * 100)}d put", "delta": -d, "strike": kp, "vol": vp})
        rows.append({"label": "ATM", "delta": 0.5, "strike": sl.strikes[2], "vol": sl.atm_vol})
        for d in sorted(deltas):
            kc, vc = sl.strike_from_delta(d, True)
            rows.append({"label": f"{int(d * 100)}d call", "delta": d, "strike": kc, "vol": vc})
        return rows

    def invalidate(self) -> None:
        self._slices.clear()
        self.atm.invalidate()
