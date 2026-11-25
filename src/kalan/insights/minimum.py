""""If I pay only the minimum": how long the debt of the latest statement lasts and what it costs.

The model, one cycle at a time (all integer kuruş, rounded half up):

1. The payment is the minimum: the printed minimum for the first statement, then ``ratio`` times the
   new statement total, rounded up, never more than the total.
2. What is left unpaid carries contractual interest for the real number of days from this cut-off
   to the next one, divided by 30: ``interest = unpaid * rate / 100 * days / 30``. Interest is
   charged on the unpaid part only, as in the bank's public examples.
3. BSMV and KKDF are each a share of the interest (15 % + 15 % by default).
4. The next statement total is ``unpaid + interest + BSMV + KKDF`` (plus, if asked, the installments
   already scheduled for that month). Repeat until the total is zero.

"Paying in full" pays the same debt (and the same installments) when due, with no interest.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from kalan.insights.dates import next_cut_offs
from kalan.insights.rules import (
    RuleOverrides,
    contractual_rate,
    minimum_ratio,
    round_half_up,
    round_up,
    tax_shares,
)
from kalan.models import Statement

MAX_CYCLES = 600


def minimum_payment_plan(
    statement: Statement,
    overrides: RuleOverrides | None = None,
    future_charges: dict[str, int] | None = None,
    max_cycles: int = MAX_CYCLES,
) -> dict[str, Any]:
    """Pay-the-minimum plan for ``statement``. ``future_charges`` maps cut-off months (``YYYY-MM``) to
    amounts added to that month's statement (for example scheduled installments)."""
    overrides = overrides or RuleOverrides()
    charges = future_charges or {}
    bsmv_share, kkdf_share = tax_shares(overrides)
    ratio = minimum_ratio(statement.credit_limit, statement.statement_total, statement.minimum_payment,
                          statement.period_end, overrides)
    printed_rate = statement.interest_rates.get("purchase_monthly")
    cuts = [statement.period_end, *next_cut_offs(statement.period_end, statement.next_period_end, max_cycles)]

    balance = max(statement.statement_total, 0)
    rows: list[dict[str, Any]] = []
    rates_used: list[dict[str, Any]] = []
    total_paid = interest_total = bsmv_total = kkdf_total = 0
    cycle = 0
    while balance > 0 and cycle < max_cycles:
        cut, next_cut = cuts[cycle], cuts[cycle + 1]
        if cycle == 0 and statement.minimum_payment > 0:
            payment = min(statement.minimum_payment, balance)
        else:
            payment = min(max(round_up(Decimal(balance) * Decimal(ratio.value)), 1), balance)
        unpaid = balance - payment
        days = (next_cut - cut).days
        rate = contractual_rate(balance, cut, printed_rate, overrides)
        if not rates_used or rates_used[-1]["monthly"] != rate.value:
            rates_used.append({"from_cycle": cycle + 1, "monthly": rate.value, "source": rate.source,
                               "detail": rate.detail})
        interest = round_half_up(Decimal(unpaid) * Decimal(rate.value) / 100 * days / 30)
        bsmv = round_half_up(Decimal(interest) * Decimal(bsmv_share.value))
        kkdf = round_half_up(Decimal(interest) * Decimal(kkdf_share.value))
        added = charges.get(f"{next_cut.year:04d}-{next_cut.month:02d}", 0)
        closing = unpaid + interest + bsmv + kkdf + added
        rows.append({"cycle": cycle + 1, "cut_off": cut.isoformat(), "balance": balance, "payment": payment,
                     "unpaid": unpaid, "days": days, "interest": interest, "bsmv": bsmv, "kkdf": kkdf,
                     "added": added, "next_balance": closing})
        total_paid += payment
        interest_total += interest
        bsmv_total += bsmv
        kkdf_total += kkdf
        balance = closing
        cycle += 1

    paid_off = balance == 0
    cost = interest_total + bsmv_total + kkdf_total
    added_total = sum(r["added"] for r in rows)
    full_total = max(statement.statement_total, 0) + added_total
    return {
        "statement_id": statement.id,
        "currency": statement.currency,
        "start_balance": max(statement.statement_total, 0),
        "minimum_only": {
            "cycles": len(rows),
            "paid_off": paid_off,
            "last_payment_cut_off": rows[-1]["cut_off"] if rows and paid_off else None,
            "total_paid": total_paid,
            "interest": interest_total,
            "bsmv": bsmv_total,
            "kkdf": kkdf_total,
            "cost": cost,
            "balance_left": balance,
        },
        "pay_in_full": {"total_paid": full_total, "cost": 0},
        "extra_cost": cost,
        "rates": {
            "contractual_monthly": rates_used,
            "bsmv": bsmv_share.model_dump(),
            "kkdf": kkdf_share.model_dump(),
            "minimum_ratio": ratio.model_dump(),
        },
        "assumptions": _assumptions(statement, bool(charges)),
        "cycles": rows,
    }


def _assumptions(statement: Statement, with_charges: bool) -> list[dict[str, str]]:
    out = [
        {"key": "minimum_paid_on_time", "text": "The minimum is paid in full by every due date, so no late interest."},
        {"key": "interest_on_unpaid_only", "text": "Interest runs on the unpaid part for the real days between "
                                                   "two cut-off dates, divided by 30, without compounding inside "
                                                   "a cycle."},
        {"key": "rates_follow_caps", "text": "Rates follow the statement's printed rate, capped by the TCMB maximum "
                                             "in force on each cut-off date; future cap changes are not known."},
    ]
    if with_charges:
        out.append({"key": "scheduled_installments_added", "text": "Installments already scheduled are added to "
                                                                    "each month's statement; nothing else is spent."})
    else:
        out.append({"key": "no_new_spending", "text": "The card is not used again until the debt is gone."})
    if statement.next_period_end is None:
        out.append({"key": "cut_off_day_kept", "text": "Later cut-off dates keep this statement's day of the "
                                                       "month (clipped in short months)."})
    return out


def latest_statement(statements: list[Statement], on: date | None = None) -> Statement | None:
    """Newest statement by cut-off date (optionally not after ``on``)."""
    pool = [s for s in statements if on is None or s.period_end <= on]
    return max(pool, key=lambda s: (s.period_end, s.id)) if pool else None
