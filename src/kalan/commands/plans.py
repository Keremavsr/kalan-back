"""``kalan takvim`` (installment schedule) and ``kalan simule`` (what if I buy this in installments)."""

from __future__ import annotations

import argparse
import re

from pydantic import ValidationError

from kalan.amounts import decimal_to_minor
from kalan.commands import Out, arg, command, money_for, shorten, table
from kalan.service import Kalan, ServiceError
from kalan.simulate import SimulationIn

_AMOUNT_RE = re.compile(r"^\d[\d.,\s]*$")


def parse_amount_arg(text: str, currency: str) -> int:
    """``9999,99``, ``9.999,99``, ``9999.99`` or ``9,999.99`` -> minor units.

    The last ``.`` or ``,`` is the decimal separator when one or two digits follow it; any other
    separator groups thousands.
    """
    raw = text.strip().replace(" ", "").replace(" ", "")
    if not _AMOUNT_RE.match(raw):
        raise ServiceError("invalid_amount", f"not an amount: {text!r}")
    cut = max(raw.rfind("."), raw.rfind(","))
    if cut >= 0 and 1 <= len(raw) - cut - 1 <= 2:
        whole, frac = raw[:cut], raw[cut + 1:]
    else:
        whole, frac = raw, ""
    whole = whole.replace(".", "").replace(",", "")
    return decimal_to_minor(f"{whole}.{frac}" if frac else whole, currency)


@command("takvim", "schedule", order=30, help="installments already committed for the coming months", args=[
    arg("-n", "--months", type=int, help="number of months (default: the months_ahead setting)"),
])
def schedule(k: Kalan, a: argparse.Namespace) -> Out:
    if a.months is not None and not 1 <= a.months <= 60:
        raise ServiceError("invalid_months", "--months must be between 1 and 60.")
    s = k.schedule(a.months)
    money = money_for(k)
    fixed = {m.month: m.total for m in s.fixed.months}
    has_fixed = any(fixed.values())
    headers = ["Month", "Installments", "Plans"] + (["Fixed costs"] if has_fixed else [])
    rows = [[m.month, money(m.total, s.currency), len(m.items)] + ([money(fixed.get(m.month, 0))] if has_fixed else [])
            for m in s.months]
    lines = table(headers, rows, right=[1, 2, 3])
    if s.plans:
        lines += ["", *table(
            ["Plan", "Source", "Each", "Left", "Ends", "Remaining"],
            [[shorten(p.description), p.source, money(p.amount_each), p.remaining_count, p.ends,
              money(p.total_remaining)] for p in s.plans],
            right=[2, 3, 5],
        )]
    lines.append(f"Amounts in {s.currency}.")
    return Out(s.model_dump(mode="json"), lines)


@command("simule", "simulate", order=70, help="what if I buy this in installments?", args=[
    arg("amount", help="purchase amount, e.g. 9.999,99"),
    arg("count", type=int, help="number of installments"),
    arg("--start", help="month of the first installment, YYYY-MM (default: the next statement month)"),
    arg("--interest", help="interest in percent per month, e.g. 3.11 (default: none)"),
    arg("-n", "--months", type=int, help="window length in months (default: covers the plan)"),
])
def simulate(k: Kalan, a: argparse.Namespace) -> Out:
    baseline = k.schedule()
    try:
        body = SimulationIn(amount=parse_amount_arg(a.amount, baseline.currency), count=a.count,
                            start_month=a.start or baseline.start_month, interest_monthly=a.interest,
                            months=a.months)
    except ValidationError as exc:
        problems = [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise ServiceError("invalid_simulation", "; ".join(problems)) from None
    result = k.simulate(body)
    money = money_for(k)
    cur = result["currency"]
    p = result["purchase"]
    each = p["installments"][0]["amount"]
    lines = [
        f"{money(p['amount'], cur)} in {p['count']} installments from {p['start_month']} to {p['ends']}:"
        f" {money(each, cur)} a month, total {money(p['total_cost'], cur)}"
        + (f" (interest {money(p['interest_total'], cur)})" if p["interest_total"] else ""),
        "",
        *table(["Month", "Now", "Purchase", "With it"],
               [[r["month"], money(r["without"]), money(r["purchase"]) if r["purchase"] else "",
                 money(r["with"])] for r in result["months"]], right=[1, 2, 3]),
    ]
    peak = result["peak"]
    if peak["with"]:
        lines.append(f"Busiest month with it: {peak['with']['month']} ({money(peak['with']['total'], cur)}).")
    return Out(result, lines)
