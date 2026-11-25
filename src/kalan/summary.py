"""Monthly summary and subscription detection. Plain integer arithmetic; no model involved.

Month attribution (the same rule as the installment schedule): a statement transaction belongs to the
cut-off month of its statement (``period_end``); a manual transaction belongs to the month of its date.

* Spend is every debit except the opening balance (``carry_over``) and payments.
* Credit is every credit except the opening balance and payments (refunds, reversals, cash back).
* Only transactions in the base currency are summed.

Ratios (``share``, ``change``, ``price_change``) are decimal strings with two places, rounded half up.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from kalan.amounts import add_months, month_key, months_between
from kalan.models import Statement, Transaction
from kalan.text import normalize_merchant

NOT_SPEND = frozenset({"carry_over", "payment"})
SUBSCRIPTION_KINDS = frozenset({"purchase", "other"})
UNCATEGORIZED = "uncategorized"
RECURRING_TOLERANCE = Decimal("0.25")


def ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(value)


def attributed(statements: list[Statement], manual: list[Transaction]) -> list[tuple[str, Transaction]]:
    """``(month, transaction)`` for every stored transaction, statement rows first."""
    out = [(month_key(st.period_end), tx) for st in statements for tx in st.transactions]
    out += [(month_key(tx.date), tx) for tx in manual]
    return out


def transactions_in_month(statements: list[Statement], manual: list[Transaction], month: str) -> list[Transaction]:
    return sorted((tx for m, tx in attributed(statements, manual) if m == month), key=lambda t: t.date)


def _totals(txs: list[Transaction], currency: str) -> tuple[int, int, dict[str, int]]:
    spend = credit = 0
    by_category: dict[str, int] = defaultdict(int)
    for tx in txs:
        if tx.currency != currency or tx.kind in NOT_SPEND:
            continue
        if tx.direction == "debit":
            spend += tx.amount
            by_category[tx.category or UNCATEGORIZED] += tx.amount
        else:
            credit += tx.amount
    return spend, credit, by_category


def monthly_summary(
    statements: list[Statement], manual: list[Transaction], month: str, currency: str
) -> dict[str, Any]:
    txs = transactions_in_month(statements, manual, month)
    spend, credit, by_category = _totals(txs, currency)
    categories = [
        {"category": c, "amount": a, "share": ratio(a, spend) or "0.00"}
        for c, a in sorted(by_category.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    statement_ids = sorted(st.id for st in statements if month_key(st.period_end) == month)

    prev_month = add_months(month, -1)
    prev_txs = transactions_in_month(statements, manual, prev_month)
    prev_has_data = bool(prev_txs) or any(month_key(st.period_end) == prev_month for st in statements)
    previous = None
    if prev_has_data:
        prev_spend = _totals(prev_txs, currency)[0]
        previous = {"total_spend": prev_spend, "change": ratio(spend - prev_spend, prev_spend)}

    return {
        "month": month,
        "currency": currency,
        "total_spend": spend,
        "total_credit": credit,
        "by_category": categories,
        "previous_month": previous,
        "statement_ids": statement_ids,
    }


def _within(previous: int, current: int) -> bool:
    if previous == 0:
        return current == 0
    return abs(Decimal(current - previous)) / Decimal(previous) <= RECURRING_TOLERANCE


def subscriptions(
    statements: list[Statement], manual: list[Transaction], model_recurring: set[str] | None = None
) -> list[dict[str, Any]]:
    """Recurring charges.

    A merchant is recurring when it was charged exactly once in each of at least two consecutive months
    and each amount is within 25% of the month before (the latest such run is reported), or when the
    model flagged one of its charges as recurring. Installment rows, fees, interest, taxes, cash
    advances, payments and refunds are never subscriptions.
    """
    flagged = model_recurring or set()
    charges: dict[tuple[str, str], dict[str, list[Transaction]]] = defaultdict(lambda: defaultdict(list))
    for month, tx in attributed(statements, manual):
        if tx.direction != "debit" or tx.kind not in SUBSCRIPTION_KINDS or tx.installment is not None:
            continue
        merchant = tx.merchant or normalize_merchant(tx.description)
        if merchant:
            charges[(merchant, tx.currency)][month].append(tx)

    items = []
    for (merchant, currency), by_month in charges.items():
        months = sorted(by_month)
        run = _latest_run(months, by_month)
        if len(run) >= 2:
            last, prev = by_month[run[-1]][0].amount, by_month[run[-2]][0].amount
            seen = run
        elif any(tx.id in flagged for txs in by_month.values() for tx in txs):
            ordered = sorted((tx for txs in by_month.values() for tx in txs), key=lambda t: t.date)
            last = ordered[-1].amount
            prev = ordered[-2].amount if len(ordered) > 1 else None
            seen = months
        else:
            continue
        items.append({
            "merchant": merchant,
            "amount_last": last,
            "amount_previous": prev,
            "currency": currency,
            "months_seen": seen,
            "price_change": None if prev is None else ratio(last - prev, prev),
        })
    return sorted(items, key=lambda i: (i["merchant"], i["currency"]))


def _latest_run(months: list[str], by_month: dict[str, list[Transaction]]) -> list[str]:
    """Latest run of consecutive single-charge months with amounts within tolerance (length >= 2), else []."""
    best: list[str] = []
    run: list[str] = []
    for m in months:
        single = len(by_month[m]) == 1
        if (
            single
            and run
            and months_between(run[-1], m) == 1
            and _within(by_month[run[-1]][0].amount, by_month[m][0].amount)
        ):
            run.append(m)
        else:
            run = [m] if single else []
        if len(run) >= 2:
            best = list(run)
    return best
