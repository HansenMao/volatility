"""The quoting agent: what it learns, what it records, and how a price is explained.

There is **one pricing engine** in this tool and it is not here: it is
``marketmaker.QuotePanel`` (§11).  The Quote button, ``volkit mm --request``
and ``volkit agent quote`` all arrive at it, and a price made in a browser and
a price made in a shell are the same price because there is one function that
makes one.  It used to be otherwise -- this module priced on its own, with its
own request grammar, a second copy of the width ladder and a second copy of the
leans -- and two engines that agreed on a Tuesday disagreed by Friday.

What lives here is everything *around* the engine that the archive makes
possible:

1. **The request and the run** -- :class:`Request` is ``volkit agent quote``'s
   settings, :func:`run` puts them through the engine and hands back
   :class:`Decision` rows: the price, and the ordered list of ingredients that
   sums to it.  The English explanation is generated from that list, and so is
   the local model's paragraph when there is one.  A story written first and
   reconciled to the numbers afterwards is a story that stays plausible when
   the numbers are wrong.
2. **The record** -- :func:`record_quote` files every price a run made as a
   ``shown`` observation, under the client it was made for, and
   :func:`answer` files what became of it.  The card's *Record* button and the
   outcome buttons beside each row, and ``volkit agent shown`` / ``outcome``,
   all call these two.
3. **Filing a paste** -- :func:`file_paste` puts the market on the screen into
   the archive, which is where the widths the engine quotes off come from.

What the agent learns, and what it is allowed to do with it (§17):

* **Widths**, per instrument and tenor bucket, from every two-way the archive
  has seen.  Applied: the archive is the second rung of the width ladder,
  below a bank rule and above the fallback tier, and the agent's verdict on the
  bank's own width sits on every row.
* **A client's record**, per instrument: which way they trade and whether the
  market follows them.  Applied, per client, capped: their side leans the mid
  and the move against us after their trades widens the price.  This is the
  one place the desk's own hit rate reaches a number, and it reaches it only
  for the caller on the phone -- a hit rate across the whole desk mixes what
  the desk was axed to do with who it was showing and says nothing a quote can
  use.
* **The market's level**, as a check on the mid and never as the mid.  A
  market maker whose mid follows the last thing it was shown is being led by
  the party it is about to trade with.
* **The printed tape**, as a lean, off unless a weight is set (``flow.py``).

And the model never gets near the arithmetic: everything above is computed
before anything is asked to describe it, and the description is refused whole
if it contains a number the decision does not (``llm.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from . import archive as arch
from . import synthesis as syn
from .knowledge import KnowledgeBank
from .marketmaker import AGENT_TOLERANCE, QuotePanel, quote_panel_from_request

DAYS_IN_YEAR = 365.2425

#: How the width was arrived at, in the order it is tried.  The order is the
#: policy: a person's rule beats a statistic, a statistic beats a number typed
#: into a box, and nothing beats saying so.
WIDTH_SOURCES = ("bank", "archive", "fallback", "none")


class AgentError(Exception):
    """A request the agent cannot answer at all."""


# --------------------------------------------------------------------------
# What was decided
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Ingredient:
    """One input to a price, with where it came from."""

    name: str
    value: float | None
    unit: str = "vol points"
    source: str = ""
    detail: str = ""
    applied: bool = True

    @classmethod
    def from_row(cls, raw: dict) -> "Ingredient":
        return cls(name=str(raw.get("name") or ""), value=raw.get("value"),
                   unit=str(raw.get("unit") or "vol points"),
                   source=str(raw.get("source") or ""), detail=str(raw.get("detail") or ""),
                   applied=bool(raw.get("applied", True)))

    def line(self) -> str:
        if self.value is None:
            body = self.detail or "not available"
            return f"{self.name}: {body}" + ("" if self.applied else " (not applied)")
        shown = f"{self.value:+.3f}" if self.name.startswith(("shading", "lean", "shift",
                                                                 "widening")) \
            else f"{self.value:.3f}"
        tail = f" -- {self.detail}" if self.detail else ""
        mark = "" if self.applied else "  (not applied)"
        return f"{self.name}: {shown} {self.unit}, {self.source}{tail}{mark}"


@dataclass
class Decision:
    """One price, and everything that set it.  A view of one quote-sheet row."""

    row: dict
    pair: str
    narration: str = ""
    narration_why_not: str = ""

    # -- the row, read once ------------------------------------------------
    @property
    def describe(self) -> str:
        return str(self.row.get("describe") or self.row.get("raw") or "")

    @property
    def model_mid(self) -> float | None:
        return self.row.get("model")

    @property
    def mid(self) -> float | None:
        return self.row.get("our_mid")

    @property
    def bid(self) -> float | None:
        return self.row.get("our_bid")

    @property
    def offer(self) -> float | None:
        return self.row.get("our_ask")

    @property
    def width(self) -> float | None:
        return self.row.get("width")

    @property
    def width_source(self) -> str:
        return str(self.row.get("width_rung") or "none")

    @property
    def shading(self) -> float:
        return float(self.row.get("skew_total") or 0.0)

    @property
    def trace(self) -> list[Ingredient]:
        return [Ingredient.from_row(x) for x in self.row.get("trace") or ()]

    @property
    def flags(self) -> list[str]:
        return list(self.row.get("flags") or ())

    @property
    def advice(self) -> list[str]:
        return list(self.row.get("advice") or ())

    @property
    def warnings(self) -> list[str]:
        return list(self.row.get("warnings") or ())

    @property
    def verdict(self) -> str:
        return str(self.row.get("agent_verdict") or "")

    @property
    def priced(self) -> bool:
        return self.bid is not None and self.offer is not None

    # -- the prose, off the record -----------------------------------------
    def quote_text(self) -> str:
        if not self.priced:
            return "no price"
        return f"{self.bid:.3f}/{self.offer:.3f}"

    def facts(self) -> list[str]:
        """The decision as lines.  This is the explanation, and the model's source.

        Everything the narration is allowed to say is here, which is what
        makes the numeric guard in ``llm.narrate`` meaningful: the set of
        permitted numbers is exactly the set of numbers a person reading this
        list would see.
        """
        out = [f"{self.pair} {self.describe}: showing {self.quote_text()}"]
        out += [f"  {ing.line()}" for ing in self.trace]
        if self.verdict and self.verdict not in ("not read", "agrees"):
            out.append(f"  width verdict: {self.verdict} -- {self.row.get('agent_note') or ''}")
        out += [f"  flag: {f}" for f in self.flags]
        out += [f"  advice: {a}" for a in self.advice]
        out += [f"  warning: {w}" for w in self.warnings]
        return out

    def explain(self) -> str:
        return "\n".join(self.facts())

    def to_json(self) -> dict:
        out = dict(self.row)
        out.update({
            "pair": self.pair, "ask": self.describe,
            "model_mid": self.model_mid, "mid": self.mid, "bid": self.bid,
            "offer": self.offer, "width_source": self.width_source,
            "shading": self.shading, "quote": self.quote_text(), "priced": self.priced,
            "narration": self.narration, "narration_why_not": self.narration_why_not,
        })
        return out


# --------------------------------------------------------------------------
@dataclass
class Request:
    """What to price, and with how much of the machinery turned on.

    Every field here is a :class:`marketmaker.QuotePanel` field under the
    name the command line gives it; :meth:`panel` is the one translation.
    """

    pair: str
    text: str = ""                      # the things to price, one per line
    cut: str = "NY"
    method: str | None = None
    fly_convention: str = "market"
    client: str = ""
    client_weight: float = 0.5
    client_min: int = syn.DEFAULT_CLIENT_MIN

    # the archive
    half_life: float = syn.DEFAULT_HALF_LIFE
    min_effective: float = syn.DEFAULT_MIN_EFFECTIVE
    lookback_days: float = 90.0
    include_model_read: bool = True
    tolerance: float = AGENT_TOLERANCE

    # the leans, exactly as the market-maker screen names them
    fair_weight: float = 0.25
    axe_weight: float = 0.5
    flow_weight: float = 0.0
    flow_scale: float = 5_000_000.0
    flow_tolerance: float = 0.03
    skew_cap: float = 1.0
    horizon_days: float = 30.0
    hist_lookback_days: float | None = None
    vega_text: str = ""
    vega_scale: float = 0.0

    # The bottom rung of the width ladder: a spreading tier off the
    # workbook's SPREADS tab, read at each row's own maturity.  See
    # `marketmaker.QuotePanel`.
    fallback_tier: str = ""
    fallback_multiplier: float = 1.0
    fallback_interpolate: bool = False
    marks: dict | None = None
    narrate: bool = True

    def panel(self) -> QuotePanel:
        """The engine's panel, through the same reader the browser's payload uses."""
        return quote_panel_from_request({
            "pair": self.pair, "cut": self.cut, "method": self.method,
            "fly_convention": self.fly_convention, "request_text": self.text,
            "marks": self.marks,
            "vega_text": self.vega_text, "vega_scale": self.vega_scale,
            "fair_weight": self.fair_weight, "axe_weight": self.axe_weight,
            "flow_weight": self.flow_weight, "flow_scale": self.flow_scale,
            "flow_tolerance": self.flow_tolerance,
            "skew_cap": self.skew_cap, "horizon_days": self.horizon_days,
            "lookback_days": self.hist_lookback_days,
            "fallback_tier": self.fallback_tier,
            "fallback_multiplier": self.fallback_multiplier,
            "fallback_interpolate": self.fallback_interpolate,
            "archive_half_life": self.half_life,
            "archive_min_effective": self.min_effective,
            "archive_lookback_days": self.lookback_days,
            "include_model_read": self.include_model_read,
            "tolerance": self.tolerance,
            "client": self.client, "client_weight": self.client_weight,
            "client_min": self.client_min,
        })


@dataclass
class AgentRun:
    """A whole answer: the decisions, the evidence behind them, and the notes."""

    pair: str
    decisions: list[Decision] = field(default_factory=list)
    synthesis: syn.Synthesis | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""
    valuation: str = ""
    client: dict = field(default_factory=dict)
    #: The engine's whole answer, exactly as the Quote button receives it.
    sheet: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "pair": self.pair, "valuation": self.valuation, "model": self.model,
            "rows": [d.to_json() for d in self.decisions],
            "evidence": (self.synthesis.lines() if self.synthesis else []),
            "proposed_rules": ([{"describe": r.describe(), "text": r.text}
                                for r in self.synthesis.proposed_rules()]
                               if self.synthesis else []),
            "client": dict(self.client),
            "notes": list(self.notes),
            "skipped": [{"line": n, "why": w, "text": t} for n, w, t in self.skipped],
            "warnings": list(self.warnings),
        }

    def text(self) -> str:
        out = [f"{self.pair}  valued {self.valuation}"]
        if self.client.get("name"):
            out.append(f"for {self.client['name']}: "
                       + ("; ".join(self.client.get("record") or [])
                          or self.client.get("reason") or "no record yet"))
        for d in self.decisions:
            out.append("")
            out.append(d.explain())
            if d.narration:
                out.append(f"  -- {d.narration}")
            elif d.narration_why_not:
                out.append(f"  -- no written explanation: {d.narration_why_not}")
        if self.synthesis:
            out += ["", "evidence"] + ["  " + x for x in self.synthesis.lines()]
        if self.skipped:
            out += ["", "not read"] + [f"  line {n} ({w}): {t}" for n, w, t in self.skipped]
        if self.notes:
            out += ["", "notes"] + [f"  {x}" for x in self.notes]
        if self.warnings:
            out += ["", "warnings"] + [f"  ! {x}" for x in self.warnings]
        return "\n".join(out)


# --------------------------------------------------------------------------
def run(request: Request, *, book, archive: arch.Archive,
        bank: KnowledgeBank | None = None, hist=None, model=None, spreads=None) -> AgentRun:
    """Make a price on everything asked for, through the one engine, and say how.

    Everything numeric is :meth:`marketmaker.QuotePanel.run`; what is added
    here is the shape the command line prints -- one decision per row with
    its trace read back -- and, when a model is running, a paragraph written
    *from* that trace.
    """
    pair = request.pair.upper()
    if book is None:
        raise AgentError("the agent needs a loaded book")
    if pair not in book:
        raise AgentError(f"{pair} is not built in this book; it holds {', '.join(book.pairs)}")
    if not str(request.text or "").strip():
        raise AgentError("nothing to price; give one instrument a line, e.g. "
                         "'1M ATM in 100mm vega'")
    try:
        panel = request.panel()
        sheet = panel.run(book, bank=bank, hist=hist, archive=archive, spreads=spreads)
    except ValueError as exc:
        raise AgentError(str(exc)) from None

    out = AgentRun(pair=pair, valuation=str(sheet.get("valuation") or ""), sheet=sheet,
                   client=dict(sheet.get("client") or {}))
    if archive is not None:
        out.synthesis = syn.synthesize(
            archive, pair, asof=book.clock.now, half_life=request.half_life,
            min_effective=request.min_effective, lookback_days=request.lookback_days,
            include_model_read=request.include_model_read)
    rows = (sheet.get("sheet") or {}).get("rows") or []
    out.notes.extend((sheet.get("sheet") or {}).get("notes") or [])
    out.notes.extend((sheet.get("archive") or {}).get("notes") or [])
    out.skipped.extend((x["line"], x["why"], x["text"])
                       for x in (sheet.get("sheet") or {}).get("skipped") or [])
    out.warnings.extend(sheet.get("warnings") or [])
    out.decisions = [Decision(row=r, pair=pair) for r in rows]
    if not rows:
        out.warnings.append("nothing was asked for that this build could read")
        return out

    if request.narrate and model is not None:
        from . import llm
        out.model = getattr(getattr(model, "config", None), "model", "")
        for d in out.decisions:
            text, why = llm.narrate(model, d.facts())
            d.narration, d.narration_why_not = text, why
    elif request.narrate:
        for d in out.decisions:
            d.narration_why_not = "no local model was configured for this run"
    return out


# --------------------------------------------------------------------------
# The record: what we showed, and what became of it
# --------------------------------------------------------------------------
def _clean_name(name) -> str:
    return " ".join(str(name or "").split())


def record_quote(archive: arch.Archive, sheet: dict, *, client: str = "",
                 at: datetime | None = None, lines=None) -> tuple[list[arch.Observation],
                                                                     list[str]]:
    """File every price on a quote sheet as a ``shown`` observation.

    This is what closes the loop.  A price that is shown and not recorded can
    never become evidence about whether we were right, and the moment to
    record it is the moment it was made -- with the mid the model had *then*,
    which is why ``model_mid`` goes on the record rather than being looked up
    when the outcome arrives.

    ``sheet`` is the engine's whole answer (the Quote button's, or
    :attr:`AgentRun.sheet`); ``lines`` narrows it to the request lines named.
    The record is kept in **the book's convention**: a row asked as ``JPY call
    over`` carries ``sign = -1`` and is turned back before it is filed, and the
    note on the record says how it was asked.  One convention in the file is
    what lets a client's record on the risk reversal be one record.
    """
    when = at or datetime.now(timezone.utc)
    # A sheet may hold several pairs (§11), each row saying which; the
    # sheet's own ``pair`` is a label then, and a row without one is filed
    # under it as before.
    sheet_pair = str(sheet.get("pair") or "").upper()
    client = _clean_name(client or (sheet.get("client") or {}).get("name"))
    wanted = None if lines is None else {int(x) for x in lines}
    written, refused = [], []
    for row in (sheet.get("sheet") or {}).get("rows") or []:
        if wanted is not None and int(row.get("line") or 0) not in wanted:
            continue
        if row.get("our_bid") is None or row.get("our_ask") is None:
            refused.append(f"line {row.get('line')} ({row.get('describe')}) has no price to "
                           f"record")
            continue
        bid, ask = float(row["our_bid"]), float(row["our_ask"])
        sign = float(row.get("sign") or 1.0)
        notes = list(row.get("flags") or ())
        if sign < 0:
            bid, ask = -ask, -bid
            notes.append(f"asked as {row.get('direction') or 'the other way round'}; filed "
                         f"in the book's convention, so lifted there is hit here")
        model_mid = row.get("model")
        pair = str(row.get("pair") or sheet_pair).upper()
        if len(pair) != 6 or not pair.isalpha():
            refused.append(f"line {row.get('line')} ({row.get('describe')}) names no pair "
                           f"to file under")
            continue
        obs = arch.shown(
            pair, instrument=str(row.get("instrument") or "atm"),
            tenor=str(row.get("tenor") or ""), tenor_far=row.get("tenor_far"),
            bid=bid, ask=ask, delta=row.get("delta"), strike=row.get("strike"),
            is_call=row.get("is_call"), fly_kind=row.get("fly_kind"),
            size=row.get("size"), size_basis=str(row.get("size_basis") or "unspecified"),
            counterparty=client,
            model_mid=None if model_mid is None else float(model_mid) * sign,
            model_note=(f"width {float(row.get('width') or 0):.3f} from the "
                        f"{row.get('width_rung') or 'none'}, shading "
                        f"{float(row.get('skew_total') or 0.0):+.3f}"),
            at=when, notes=tuple(notes))
        obs = replace(obs, raw=str(row.get("raw") or ""), line=int(row.get("line") or 0))
        ok, why = archive.add(obs)
        if ok:
            written.append(obs)
        else:
            refused.append(f"line {row.get('line')}: {why}")
    return written, refused


#: What an outcome may say, and which of them a *sign* turns round.  The
#: buttons on the sheet speak the row's own convention -- "they lifted our
#: offer" on a `JPY call over` row -- and the file speaks the book's, so the
#: two trade names are swapped on the way in for a row asked the other way.
_TURNED = {"traded_bid": "traded_ask", "traded_ask": "traded_bid"}


def answer(archive: arch.Archive, ref: str, result: str, *, away_level: float | None = None,
           sign: float = 1.0, client: str = "", at: datetime | None = None) -> arch.Observation:
    """File what became of a price we showed.

    ``ref`` is the ``shown`` record's id, ``result`` one of
    :data:`archive.RESULTS` **in the convention the price was shown in** --
    ``sign`` says which that was, and a negative one swaps the two trade
    names on the way into the file.  ``away_level`` is where it went if it
    went away, in the shown convention too.
    """
    result = str(result or "").strip().lower()
    if result not in arch.RESULTS:
        raise AgentError(f"an outcome is one of {', '.join(arch.RESULTS)}, not {result!r}")
    target = archive.by_id(str(ref or "").strip())
    if target is None:
        raise AgentError(f"no record in the archive has the id {ref!r}")
    if target.kind != "shown":
        raise AgentError(f"{ref} is a {target.kind} record, not a price we showed; an outcome "
                         f"answers a price we made")
    notes: list[str] = []
    if sign < 0:
        turned = _TURNED.get(result, result)
        if turned != result:
            notes.append(f"answered as {result} in the convention the price was asked in; "
                         f"filed as {turned} in the book's")
        result = turned
        if away_level is not None:
            away_level = -float(away_level)
    obs = arch.outcome(target, result, away_level=away_level, at=at or datetime.now(timezone.utc),
                       counterparty=_clean_name(client), notes=tuple(notes))
    ok, why = archive.add(obs)
    if not ok:
        raise AgentError(why)
    return obs


def record_from_request(archive: arch.Archive, payload: dict, *, clock) -> dict:
    """The sheet's *Record* button: the quote answer it holds, filed under the client."""
    sheet = payload.get("sheet")
    if not isinstance(sheet, dict) or not isinstance(sheet.get("sheet"), dict):
        raise AgentError("nothing to record: press Quote first, then Record")
    lines = payload.get("lines")
    if lines is not None and not isinstance(lines, (list, tuple)):
        raise AgentError("lines must be a list of request line numbers")
    written, refused = record_quote(archive, sheet, client=str(payload.get("client") or ""),
                                    at=clock.now, lines=lines)
    flushed = archive.flush()
    return {
        "pair": str(sheet.get("pair") or "").upper(),
        "client": _clean_name(payload.get("client") or (sheet.get("client") or {}).get("name")),
        "recorded": [{"id": o.id, "line": o.line, "describe": o.describe(),
                      "bid": o.bid, "ask": o.ask} for o in written],
        "refused": refused, "written": flushed, "path": archive.path,
    }


def outcome_from_request(archive: arch.Archive, payload: dict, *, clock) -> dict:
    """One outcome button, pressed."""
    away = payload.get("away_level")
    try:
        away = None if away in (None, "") else float(away)
    except (TypeError, ValueError):
        raise AgentError(f"away_level must be a number, not {away!r}") from None
    try:
        sign = float(payload.get("sign") or 1.0)
    except (TypeError, ValueError):
        raise AgentError(f"sign must be a number, not {payload.get('sign')!r}") from None
    obs = answer(archive, str(payload.get("ref") or ""), str(payload.get("result") or ""),
                 away_level=away, sign=sign, client=str(payload.get("client") or ""),
                 at=clock.now)
    flushed = archive.flush()
    return {"id": obs.id, "ref": obs.ref, "result": obs.result, "describe": obs.describe(),
            "written": flushed, "path": archive.path}


# --------------------------------------------------------------------------
# The archive, from the screen
# --------------------------------------------------------------------------
def _archive_block(archive: arch.Archive, pair: str, now: datetime) -> dict:
    """What the file holds for this pair, and how fresh it is."""
    rows = [r for r in archive.summary() if r["pair"] == pair.upper()]
    block = {"path": archive.path, "records": 0, "quote": 0, "trade": 0, "shown": 0,
             "outcome": 0, "last": "", "first": "", "model_read": 0, "age_days": None,
             "pairs": len(archive.pairs())}
    if rows:
        block.update(rows[0])
        newest = arch.parse_time(block.get("last") or "")
        if newest is not None:
            block["age_days"] = round(
                max(0.0, (now - newest).total_seconds() / 86400.0), 3)
    return block


def _rounded(value, places: int = 6):
    """A float the page can print and two runs can compare.

    The screen formats to three places anyway; what this is really for is the
    JSON, which is read by people and diffed by tests, and where
    ``0.39999999999999947`` is the same number wearing a disguise.
    """
    if value is None:
        return None
    try:
        out = round(float(value), places)
    except (TypeError, ValueError):
        return None
    return None if out != out else out          # nan carries no information here


@dataclass
class Paste:
    """The market on the screen, as the file button posts it.

    ``pair`` names the one pair the paste is read as (the command line's);
    empty, the paste names its pairs itself -- on the line or under a heading
    -- and a line naming none is refused rather than read as anybody's, which
    is how the market-maker screen's box works now that it has no pair
    selector (§11).
    """

    pair: str = ""
    text: str = ""
    fly_convention: str = "market"
    vol_unit: str = "auto"


def paste_from_request(payload: dict) -> Paste:
    """The paste as the browser posts it: the pair (if one), the text and its conventions.

    The same discipline as every other reader (§4): the browser posts the
    panel whole, and a field it sends that is not read here is a setting that
    silently does nothing, so a test pins the page's list against this.
    """
    return Paste(pair=str(payload.get("pair") or "").strip().upper(),
                 text=str(payload.get("text") or ""),
                 fly_convention=str(payload.get("fly_convention") or "market"),
                 vol_unit=str(payload.get("vol_unit") or "auto"))


def paste_runs(paste: Paste, *, today) -> tuple[list[tuple[str, object]], list[dict]]:
    """The paste read once per pair: ``[(pair, ParsedRun)]`` and the lines naming none.

    With a pair on the paste that is the one run, read as it always was.
    Without one, the pairs are the paste's own (``marketmaker.pairs_named``)
    and each is read with the other pairs' lines passed over and a bare line
    refused -- the same reading the check and the quote sheets give the box.
    """
    from .marketmaker import pairs_named
    from .quotes import parse_quotes
    text = str(paste.text or "")
    if paste.pair:
        run_ = parse_quotes(text, pair=paste.pair, vol_unit=paste.vol_unit,
                            fly_convention=paste.fly_convention, today=today)
        return [(paste.pair, run_)], []
    pairs, bare = pairs_named(text, today=today, fly_convention=paste.fly_convention)
    runs = []
    for pair in pairs:
        runs.append((pair, parse_quotes(text, pair=pair, vol_unit=paste.vol_unit,
                                        fly_convention=paste.fly_convention, today=today,
                                        require_pair=True)))
    return runs, bare


@dataclass
class Learned:
    """What the archive proposes as bank widths, and what it counted."""

    rules: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    counted: int = 0
    from_paste: int = 0
    skipped: list = field(default_factory=list)


def learn_widths(archive: arch.Archive, pair: str, *, clock, paste: Paste | None = None,
                 counterparty: str = "", half_life: float = syn.DEFAULT_HALF_LIFE,
                 min_effective: float = syn.DEFAULT_MIN_EFFECTIVE, lookback_days: float = 90.0,
                 include_model_read: bool = True) -> Learned:
    """Propose bank widths from the archive, with the paste on the screen counted.

    **One pipeline for a width into the bank.**  There were two: the paste's
    own widths (a plain median of what was on the screen, `volkit mm
    --learn`) and the archive's (age-weighted, with a floor on the evidence,
    `volkit agent learn`), and the second was not reachable from the screen
    at all.  This is the second, with the paste counted **unfiled**: its
    quotes are added to a copy of the archive for the length of this call, so
    a run that has not been filed still teaches, and nothing is written --
    proposing is not filing, and filing is the *File this run* button.  The
    archive's own ids keep a run that *has* been filed from counting twice.
    """
    from .quotes import parse_quotes
    out = Learned()
    scratch = arch.Archive(path="")
    scratch.extend(archive.records)
    if paste is not None and str(paste.text or "").strip():
        # This pair's lines of the paste.  A paste with no pair of its own is
        # the screen's box, which names its pairs itself, so a bare line is
        # refused here exactly as the sheet refuses it.
        run_ = parse_quotes(paste.text, pair=pair, vol_unit=paste.vol_unit,
                            fly_convention=paste.fly_convention, today=clock.now.date(),
                            require_pair=not paste.pair)
        # A line with no clock on it is stamped at the start of the valuation
        # day when it is *filed* (``file_paste``), so a run filed twice files
        # once; that stamp is used here only to ask whether a line is already
        # in the archive.  What is counted is stamped **now**: a run pasted
        # this morning is this morning's evidence, at full weight, and not
        # half a day old before anybody has read it.
        day = clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
        filed = arch.from_quotes(run_, pair=pair, source="chat",
                                 origin="pasted on the market-maker screen",
                                 counterparty=counterparty, via="hand", default_time=day)
        fresh = arch.from_quotes(run_, pair=pair, source="chat",
                                 origin="pasted on the market-maker screen",
                                 counterparty=counterparty, via="hand", default_time=clock.now)
        rows = [now_ for was, now_ in zip(filed, fresh) if was not in archive]
        added, refused = scratch.extend(rows)
        out.from_paste = added
        # (line, text, why) is what the parser returns, and this used to
        # unpack it as (line, why, text): the card showed the reason where the
        # line should be and the line where the reason should be.
        out.skipped = [{"line": n, "text": text, "why": why} for n, text, why in run_.skipped]
        # On a paste that names its pairs itself the other pairs' lines are
        # not "passed over", they are the other pairs'.
        out.notes.extend(n for n in run_.notes
                         if paste.pair or "quote another pair and were passed over" not in n)
        if added:
            out.notes.append(f"{added} quote(s) from the paste were counted without being "
                             f"filed; File this run to keep them")
        elif rows:
            out.notes.append("every quote in the paste is already in the archive")
    made = syn.synthesize(scratch, pair, asof=clock.now, half_life=half_life,
                          min_effective=min_effective, lookback_days=lookback_days,
                          include_model_read=include_model_read)
    out.counted = made.counted
    out.rules = made.proposed_rules()
    out.notes.extend(made.notes)
    if not out.rules:
        thin = [w for w in made.widths if not w.enough]
        out.notes.append(
            "nothing has enough behind it to propose a width"
            + (f": {'; '.join(w.describe() for w in thin[:4])}" if thin else
               "; the archive holds no two-way for this pair in the window"))
    return out


def learn_from_request(archive: arch.Archive, payload: dict, *, clock) -> dict:
    """The bank card's *Learn widths* button.  Proposes; the person saves.

    One pair (``pair``) answers as it always did.  Several (``pairs``, the
    bank card's every pair) answer one by one under ``by_pair``, each from
    the archive and its own lines of the paste -- the paste keeps no pair of
    its own then, so a bare line is refused rather than counted for every
    pair in turn -- with the proposed rules carrying their pair so the card
    can put each under its own table.
    """
    paste = paste_from_request(payload)

    def number(name, default):
        raw = payload.get(name, None)
        if raw in (None, "", "none"):
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise AgentError(f"{name} must be a number, not {raw!r}") from None

    raw_flag = payload.get("include_model_read", True)
    flag = (raw_flag.strip().lower() not in ("", "0", "no", "off", "false")
            if isinstance(raw_flag, str) else bool(raw_flag))
    settings = dict(
        counterparty=str(payload.get("counterparty") or "").strip(),
        half_life=number("archive_half_life", syn.DEFAULT_HALF_LIFE),
        min_effective=number("archive_min_effective", syn.DEFAULT_MIN_EFFECTIVE),
        lookback_days=number("archive_lookback_days", 90.0),
        include_model_read=flag)

    wanted = payload.get("pairs")
    if wanted is None:
        if not paste.pair:
            raise AgentError("the quoting agent needs a pair, or a list of pairs")
        return _learned_json(paste.pair,
                             learn_widths(archive, paste.pair, clock=clock, paste=paste,
                                          **settings))
    if not isinstance(wanted, (list, tuple)):
        raise AgentError("pairs must be a list of currency pairs")
    pairs = [str(p).strip().upper() for p in wanted if str(p or "").strip()]
    if not pairs:
        raise AgentError("pairs is empty: name the pairs to learn widths for")
    # The paste is read per pair without a pair of its own, so its lines go
    # to the pair they name and nowhere else.
    shared = Paste(pair="", text=paste.text, fly_convention=paste.fly_convention,
                   vol_unit=paste.vol_unit)
    by_pair = {one: _learned_json(one, learn_widths(archive, one, clock=clock, paste=shared,
                                                    **settings))
               for one in pairs}
    bare: list[dict] = []
    if str(paste.text or "").strip():
        from .marketmaker import pairs_named
        _, bare = pairs_named(paste.text, today=clock.now.date(),
                              fly_convention=paste.fly_convention)
    return {
        "pairs": pairs, "by_pair": by_pair,
        "rules": [dict(r, pair=p) for p, one in by_pair.items() for r in one["rules"]],
        "describe": [f"{p}: {d}" for p, one in by_pair.items() for d in one["describe"]],
        "notes": [f"{p}: {n}" for p, one in by_pair.items() for n in one["notes"]]
                 + ([f"{len(bare)} line(s) of the paste name no pair and were not counted"]
                    if bare else []),
        "counted": sum(one["counted"] for one in by_pair.values()),
        "from_paste": sum(one["from_paste"] for one in by_pair.values()),
        "skipped": bare,
    }


def _learned_json(pair: str, got: Learned) -> dict:
    from dataclasses import asdict
    return {"pair": pair, "rules": [asdict(r) for r in got.rules],
            "describe": [r.describe() for r in got.rules], "notes": got.notes,
            "counted": got.counted, "from_paste": got.from_paste, "skipped": got.skipped}


def file_paste(archive: arch.Archive, payload: dict, *, clock,
               counterparty: str = "") -> dict:
    """Put the run currently on the screen into the archive.

    The timestamp a line with no clock on it gets is **the start of the
    valuation day**, not the instant the button was pressed.  The id is a hash
    of the content, so "now" would give the same run a new id every time it
    was filed and a morning double-clicked would count twice in every width
    it touches.  Midnight of the valuation day makes filing a run twice in a
    day file it once, and the day is all the resolution an age weight
    measured in days can use anyway.
    """
    panel = paste_from_request(payload)
    if not str(panel.text or "").strip():
        raise AgentError("there is nothing pasted to file")
    runs, bare = paste_runs(panel, today=clock.now.date())
    if not runs:
        raise AgentError("nothing in the paste names a pair; write it on the line or as a "
                         "heading line above the quotes")
    day = clock.now.replace(hour=0, minute=0, second=0, microsecond=0)
    observations: list[arch.Observation] = []
    notes: list[str] = []
    skipped: list[dict] = []
    seen_lines: set[int] = set()
    under_another_name = 0
    for pair, run_ in runs:
        rows = arch.from_quotes(
            run_, pair=pair, source="chat", origin="pasted on the market-maker screen",
            counterparty=counterparty, via="hand", default_time=day)
        # The broker's name is part of what makes an observation distinct --
        # the same width from three brokers is stronger evidence than three
        # quotes from one -- so filing the same run again under a different
        # name is a genuinely new record and not a duplicate.  It is also the
        # obvious way to double a width by accident, so it is counted and said
        # out loud.
        anonymous = {replace(o, counterparty="").id
                     for o in archive.query(pair=pair, kinds="quote")}
        under_another_name += sum(1 for o in rows
                                  if o.id not in archive._ids
                                  and replace(o, counterparty="").id in anonymous)
        observations.extend(rows)
        prefix = f"{pair}: " if len(runs) > 1 or not panel.pair else ""
        notes.extend(prefix + n for n in run_.notes
                     if "quote another pair and were passed over" not in n)
        for n, text, why in run_.skipped:
            if n not in seen_lines:
                seen_lines.add(n)
                skipped.append({"line": n, "text": text, "why": why})
    for x in bare:
        if x["line"] not in seen_lines:
            seen_lines.add(x["line"])
            skipped.append({"line": x["line"], "why": x["why"], "text": x["text"]})
    skipped.sort(key=lambda x: x["line"])
    added, refused = archive.extend(observations)
    written = archive.flush()
    if under_another_name:
        notes.append(
            f"{under_another_name} of these quote(s) are already in the archive under a "
            f"different broker name and have been filed again; that is right when two brokers "
            f"really showed the same market, and doubles the evidence behind a width when it "
            f"was the same run filed twice")
    pairs = [p for p, _ in runs]
    return {
        "pair": ", ".join(pairs), "pairs": pairs,
        "read": len(observations), "added": added,
        "already_held": len(observations) - added - len(refused),
        "under_another_name": under_another_name,
        "refused": refused, "written": written, "path": archive.path,
        "notes": notes,
        "skipped": skipped,
    }
