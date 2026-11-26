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

from kalan.amounts import add_months, month_key
from kalan.models import Statement, Transaction

NOT_SPEND = frozenset({"carry_over", "payment"})
UNCATEGORIZED = "uncategorized"


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


def subscriptions(
    statements: list[Statement], manual: list[Transaction], model_recurring: set[str] | None = None
) -> list[dict[str, Any]]:
    """Regular payments (subscriptions and bills): see :func:`kalan.regular.regular_payments`."""
    from kalan.regular import regular_payments  # late: kalan.regular imports this module

    return regular_payments(statements, manual, model_recurring)
