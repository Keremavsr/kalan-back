"""``kalan takvim`` (installment schedule) and ``kalan simule`` (what if I buy this in installments)."""

from __future__ import annotations

import argparse
import re

from pydantic import ValidationError

from kalan.amounts import decimal_to_minor
from kalan.commands import Out, arg, cols, command, money_for, shorten, table
from kalan.i18n import t, word
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


@command("takvim", "schedule", order=30, help="help.takvim", args=[
    arg("-n", "--months", type=int, help="help.months"),
])
def schedule(k: Kalan, a: argparse.Namespace) -> Out:
    if a.months is not None and not 1 <= a.months <= 60:
        raise ServiceError("invalid_months", "--months must be between 1 and 60.")
    s = k.schedule(a.months)
    money = money_for(k)
    fixed = {m.month: m.total for m in s.fixed.months}
    has_fixed = any(fixed.values())
    headers = cols("month", "installments", "plans") + (cols("fixed") if has_fixed else [])
    rows = [[m.month, money(m.total, s.currency), len(m.items)] + ([money(fixed.get(m.month, 0))] if has_fixed else [])
            for m in s.months]
    lines = table(headers, rows, right=[1, 2, 3])
    if s.plans:
        lines += ["", *table(
            cols("plan", "source", "each", "left", "ends", "remaining"),
            [[shorten(p.description), word("source", p.source), money(p.amount_each), p.remaining_count, p.ends,
              money(p.total_remaining)] for p in s.plans],
            right=[2, 3, 5],
        )]
    lines.append(t("amounts_in", currency=money.unit(s.currency)))
    return Out(s.model_dump(mode="json"), lines)


@command("simule", "simulate", order=70, help="help.simule", args=[
    arg("amount", help="help.simule.amount"),
    arg("count", type=int, help="help.simule.count"),
    arg("--start", help="help.simule.start"),
    arg("--interest", help="help.simule.interest"),
    arg("-n", "--months", type=int, help="help.simule.months"),
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
    head = t("simulate.head", amount=money(p["amount"], cur), count=p["count"], start=p["start_month"],
             end=p["ends"], each=money(each, cur), total=money(p["total_cost"], cur))
    if p["interest_total"]:
        head += t("simulate.interest", interest=money(p["interest_total"], cur))
    lines = [
        head,
        "",
        *table(cols("month", "now", "purchase", "with"),
               [[r["month"], money(r["without"]), money(r["purchase"]) if r["purchase"] else "",
                 money(r["with"])] for r in result["months"]], right=[1, 2, 3]),
    ]
    peak = result["peak"]
    if peak["with"]:
        lines.append(t("simulate.peak", month=peak["with"]["month"], total=money(peak["with"]["total"], cur)))
    return Out(result, lines)
