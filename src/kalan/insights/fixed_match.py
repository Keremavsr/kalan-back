"""Link statement rows to fixed costs with a tolerance, and suggest duplicate fixed costs.

A debit row matches a fixed cost when it is in the same currency, its amount is within 7.5 % of the
fixed cost's amount, and its date is within 2 days of the fixed cost's day (the day is clipped to short
months, and the distance may cross a month end). Each row links to at most one fixed cost and each
fixed cost to at most one row per month; the closest date wins, then the closest amount.

Two fixed costs look like duplicates when their periods overlap and they would match the same row:
same currency, amounts within 7.5 % of each other and days within 2 of each other.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from kalan.amounts import add_months, month_key, months_between
from kalan.insights.dates import clip_day, shift_month
from kalan.models import FixedCost, Statement, Transaction
from kalan.summary import NOT_SPEND

AMOUNT_TOLERANCE = Decimal("0.075")
DAY_TOLERANCE = 2


def _active(cost: FixedCost, month: str) -> bool:
    return months_between(cost.start, month) >= 0 and (cost.end is None or months_between(month, cost.end) >= 0)


def _due_near(cost: FixedCost, d: date) -> tuple[date, int]:
    """The fixed cost's due date closest to ``d`` (this, previous or next month) and the day distance."""
    base = date(d.year, d.month, 1)
    options = [clip_day(x.year, x.month, cost.day) for x in (shift_month(base, -1), base, shift_month(base, 1))]
    due = min(options, key=lambda o: abs((o - d).days))
    return due, abs((due - d).days)


def _amount_ok(expected: int, actual: int, tolerance: Decimal) -> bool:
    return abs(Decimal(actual - expected)) <= Decimal(expected) * tolerance


def match_fixed_costs(
    costs: list[FixedCost],
    transactions: list[Transaction],
    amount_tolerance: Decimal = AMOUNT_TOLERANCE,
    day_tolerance: int = DAY_TOLERANCE,
) -> list[dict[str, Any]]:
    """Links ``{"fixed_cost_id","month","transaction_id","date","amount","amount_diff","day_diff"}`` where
    ``month`` is the fixed cost's due month."""
    candidates = []
    for t in transactions:
        if t.direction != "debit" or t.kind in NOT_SPEND or t.installment is not None:
            continue
        for c in costs:
            if t.currency != c.currency or not _amount_ok(c.amount, t.amount, amount_tolerance):
                continue
            due, days = _due_near(c, t.date)
            if days <= day_tolerance and _active(c, month_key(due)):
                candidates.append((days, abs(t.amount - c.amount), t, c, due))
    candidates.sort(key=lambda x: (x[0], x[1], x[2].date, x[2].id, x[3].id))
    used_tx: set[str] = set()
    used_slot: set[tuple[str, str]] = set()
    links = []
    for days, diff, t, c, due in candidates:
        slot = (c.id, month_key(due))
        if t.id in used_tx or slot in used_slot:
            continue
        used_tx.add(t.id)
        used_slot.add(slot)
        links.append({"fixed_cost_id": c.id, "month": month_key(due), "transaction_id": t.id,
                      "date": t.date.isoformat(), "amount": t.amount, "amount_diff": t.amount - c.amount,
                      "day_diff": (t.date - due).days, "_abs": (days, diff)})
    for link in links:
        link.pop("_abs")
    return sorted(links, key=lambda x: (x["fixed_cost_id"], x["month"]))


def _day_gap(a: int, b: int) -> int:
    gap = abs(a - b)
    return min(gap, 31 - gap)


def _overlap(a: FixedCost, b: FixedCost) -> bool:
    a_end, b_end = a.end or "9999-12", b.end or "9999-12"
    return months_between(a.start, b_end) >= 0 and months_between(b.start, a_end) >= 0


def duplicate_fixed_costs(costs: list[FixedCost], amount_tolerance: Decimal = AMOUNT_TOLERANCE,
                          day_tolerance: int = DAY_TOLERANCE) -> list[dict[str, Any]]:
    out = []
    for i, a in enumerate(costs):
        for b in costs[i + 1:]:
            if a.currency != b.currency or not _overlap(a, b) or _day_gap(a.day, b.day) > day_tolerance:
                continue
            if not _amount_ok(max(a.amount, b.amount), min(a.amount, b.amount), amount_tolerance):
                continue
            out.append({"fixed_cost_ids": [a.id, b.id], "same_name": a.name.casefold() == b.name.casefold(),
                        "amount_diff": b.amount - a.amount, "day_gap": _day_gap(a.day, b.day)})
    return out


def fixed_cost_report(costs: list[FixedCost], statements: list[Statement],
                      loose: list[Transaction]) -> dict[str, Any]:
    """Matches per fixed cost, months with data but no matching row, and duplicate suggestions."""
    txs = [t for st in statements for t in st.transactions] + list(loose)
    links = match_fixed_costs(costs, txs)
    seen_months = sorted({month_key(t.date) for t in txs})
    # every month between the first and the last month with data; the two ends may be partial
    inner = ([add_months(seen_months[0], i) for i in range(1, months_between(seen_months[0], seen_months[-1]))]
             if seen_months else [])
    report = []
    for c in costs:
        mine = [link for link in links if link["fixed_cost_id"] == c.id]
        seen = {link["month"] for link in mine}
        missing = [m for m in inner if _active(c, m) and m not in seen]
        report.append({"fixed_cost_id": c.id, "name": c.name, "amount": c.amount, "currency": c.currency,
                       "day": c.day, "matches": mine, "missing_months": missing})
    return {
        "tolerance": {"amount": str(AMOUNT_TOLERANCE), "days": DAY_TOLERANCE},
        "costs": report,
        "duplicates": duplicate_fixed_costs(costs),
    }


def matched_transaction_ids(costs: list[FixedCost], statements: list[Statement],
                            loose: list[Transaction]) -> set[str]:
    txs = [t for st in statements for t in st.transactions] + list(loose)
    return {link["transaction_id"] for link in match_fixed_costs(costs, txs)}
