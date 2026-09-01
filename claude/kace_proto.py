"""Prototype: build the kACE RATE_FEED gfi_message that the XML-poster
workbook produces, straight from the numbers volkit already has.

Inputs mirror what the workbook consumes:
  * daily   -- {date: cumulative vol in %} (the "save day vol" file, tenor ticked)
  * pillars -- per quoted tenor: expiry date, ATM bid/offer spread (vol pts),
               25/10 RR and 25/10 FLY in vol points (Murex layout)
Everything else in the sheet is layout.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from xml.sax.saxutils import quoteattr


@dataclass(frozen=True)
class Pillar:
    tenor: str
    expiry: date
    atm_spread: float      # vol points, full bid/offer width
    rr25: float            # vol points, kACE "$ call" convention as marked
    rr10: float
    fly25: float
    fly10: float


def _num(x: float) -> str:
    """A decimal the way the sheet writes one: no exponent, no trailing zeros."""
    s = f"{x:.15g}"
    if "e" in s or "E" in s:
        s = f"{x:.20f}".rstrip("0").rstrip(".")
    return s


def _d(d: date) -> str:
    return d.strftime("%d %b %Y")


def spread_for(day: date, pillars: list[Pillar]) -> float:
    """The sheet's approximate VLOOKUP: the last pillar whose expiry <= day,
    and the first pillar's spread before any expiry."""
    chosen = pillars[0].atm_spread
    for p in sorted(pillars, key=lambda p: p.expiry):
        if p.expiry <= day:
            chosen = p.atm_spread
        else:
            break
    return chosen


def _node(name: str, ccy: str, ctr: str, maturity: date, fields: list[tuple[str, str]]) -> list[str]:
    out = [f'      <node name="{name}">',
           '        <field name="RateType" value="Volatility"/>',
           f'        <field name="Currency" value="{ccy}"/>',
           f'        <field name="CtrCcy" value="{ctr}"/>',
           f'        <field name="Maturity" value="{_d(maturity)}"/>']
    out += [f'        <field name={quoteattr(k)} value={quoteattr(v)}/>' for k, v in fields]
    out.append('      </node>')
    return out


def rate_feed_xml(pair: str, daily: dict[date, float], pillars: list[Pillar], *,
                  hor_date: date, username: str, password: str,
                  scenario: str = "Xyz", transaction_id: str = "1234567890",
                  timestamp: datetime | None = None, clear: bool = False) -> str:
    ccy, ctr = pair[:3].upper(), pair[3:6].upper()
    ts = (timestamp or datetime.now().astimezone()).strftime("%Y-%m-%dT%H:%M:%S%z")
    ts = ts[:-2] + ":" + ts[-2:] if len(ts) > 5 and ts[-5] in "+-" else ts
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gfi_message version="2.0">',
             '  <header>',
             f'    <transactionId>{transaction_id}</transactionId>',
             f'    <timestamp>{ts}</timestamp>',
             f'    <username>{username}</username>',
             f'    <password>{password}</password>',
             '  </header>',
             '  <body>',
             '    <action name="action1" function="RATE_FEED" version="1.0">',
             '      <option name="data" ref="data1"/>',
             f'      <option name="scenario" value="{scenario}"/>',
             f'      <option name="horDate" value="{_d(hor_date)}"/>']
    if clear:
        lines.append('      <option name="clearRate" value="true"/>')
    lines += ['    </action>', '    <data name="data1" format="NAME_VALUE">']
    if clear:
        lines += [f'      <node name="{ccy}{ctr}">',
                  '        <field name="RateType" value="Volatility"/>',
                  f'        <field name="Currency" value="{ccy}"/>',
                  f'        <field name="CtrCcy" value="{ctr}"/>',
                  '      </node>']
    else:
        pillars = sorted(pillars, key=lambda p: p.expiry)
        missing = [p.tenor for p in pillars if p.expiry not in daily]
        if missing:
            raise ValueError(f"the daily series does not reach the {', '.join(missing)} expiry; "
                             f"extend the horizon (the sheet would have posted #N/A here)")
        n = 0
        for day, vol in sorted(daily.items()):
            n += 1
            half = spread_for(day, pillars) / 2.0
            lines += _node(str(n), ccy, ctr, day, [
                ("VolType", "ATM"),
                ("Volity", f"{_num((vol - half) / 100)}/{_num((vol + half) / 100)}")])
        s = 0
        for p in pillars:
            vol, half = daily[p.expiry], p.atm_spread / 2.0
            s += 1
            lines += _node(f"S{s}", ccy, ctr, p.expiry, [
                ("VolType", "ATM"),
                ("Volity", f"{_num((vol - half) / 100)}/{_num((vol + half) / 100)}")])
            for delta, tag, kind, val in ((0.25, "0.25", "RR", p.rr25), (0.10, "0.10", "RR", p.rr10),
                                          (0.25, "0.25", "S", p.fly25), (0.10, "0.10", "S", p.fly10)):
                s += 1
                lines += _node(f"S{s}", ccy, ctr, p.expiry, [
                    ("PctDelta", tag), ("VolType", kind), ("Volity", _num(val / 100))])
    lines += ['    </data>', '  </body>', '</gfi_message>']
    return "\n".join(lines) + "\n"
