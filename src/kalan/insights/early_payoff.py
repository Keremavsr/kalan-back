"""Early payoff: what closing an installment plan early would cost, and plans already closed early.

kalan does not know any bank's early payoff (erken kapama) discount rule, so it never invents one:

* a purchase plan: the plan carries no separate interest, so the payoff is the plain remaining total;
* a cash advance plan with a printed split (principal, vade farkı, BSMV, KKDF): two figures are given,
  the plain remaining total and the remaining principal alone. The second applies only if the bank
  drops the future interest and its taxes; the response says the rule is unknown.

Months follow the schedule: paying off in month ``M`` means the installments billed before ``M`` are
paid as usual and all the plan's installments from ``M`` on are billed together in ``M``.
"""

from __future__ import annotations

from typing import Any

from kalan.amounts import months_between
from kalan.models import Statement
from kalan.schedule import Schedule

UNKNOWN_RULE = ("kalan does not know this bank's early payoff rule. Ask the bank for the payoff amount; "
                "it is at most the remaining total shown here.")


def payoff(schedule: Schedule, plan_id: str, month: str | None = None) -> dict[str, Any] | None:
    """Effect of paying off ``plan_id`` in ``month`` (default: the schedule's first month). ``None`` if the
    plan is not in the schedule."""
    plan = next((p for p in schedule.plans if p.plan_id == plan_id), None)
    if plan is None:
        return None
    month = month or schedule.start_month
    items = [(m.month, i) for m in schedule.months for i in m.items if i.plan_id == plan_id]
    later = [(mk, i) for mk, i in items if months_between(month, mk) >= 0]
    # installments beyond the schedule window are still part of the plan
    beyond = plan.remaining_count - len(items)
    remaining_count = len(later) + max(beyond, 0)
    remaining_total = remaining_count * plan.amount_each
    out: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "description": plan.description,
        "kind": plan.kind,
        "source": plan.source,
        "currency": schedule.currency,
        "payoff_month": month,
        "remaining_count": remaining_count,
        "amount_each": plan.amount_each,
        "remaining_total": remaining_total,
        "discount_rule": "unknown",
        "note": UNKNOWN_RULE,
    }
    if plan.kind == "cash_advance" and plan.breakdown is not None and plan.remaining_count:
        each = plan.breakdown.model_dump()
        each = {k: v // plan.remaining_count for k, v in each.items()}  # breakdown is per remaining total
        parts = {k: v * remaining_count for k, v in each.items()}
        out["breakdown"] = parts
        out["remaining_principal"] = parts["principal"]
        out["would_save_if_future_interest_dropped"] = remaining_total - parts["principal"]
    else:
        out["breakdown"] = None
        out["remaining_principal"] = remaining_total if plan.kind == "purchase" else None
        out["would_save_if_future_interest_dropped"] = 0 if plan.kind == "purchase" else None
    effect = []
    for m in schedule.months:
        in_plan = sum(i.amount for i in m.items if i.plan_id == plan_id)
        if months_between(month, m.month) < 0:
            with_payoff = m.total
        elif m.month == month:
            with_payoff = m.total - in_plan + remaining_total
        else:
            with_payoff = m.total - in_plan
        effect.append({"month": m.month, "without": m.total, "with": with_payoff})
    out["schedule_effect"] = effect
    return out


def closed_early(statements: list[Statement]) -> list[dict[str, Any]]:
    """Plans a statement shows as closed before their last installment: an ``early_payoff`` row, or a
    row marked last whose installment number is below the plan length."""
    found: dict[str, dict[str, Any]] = {}
    for st in sorted(statements, key=lambda s: s.period_end):
        for t in st.transactions:
            info = t.installment
            if info is None:
                continue
            early = info.component == "early_payoff" or (
                info.is_last and info.index is not None and info.total_count is not None
                and info.index < info.total_count)
            if early and info.plan_id not in found:
                found[info.plan_id] = {"plan_id": info.plan_id, "statement_id": st.id,
                                       "month": st.cut_off_month, "transaction_id": t.id,
                                       "index": info.index, "total_count": info.total_count}
    return list(found.values())


def payoff_overview(schedule: Schedule, statements: list[Statement]) -> dict[str, Any]:
    """Payoff in the first schedule month for every open plan, plus plans already closed early."""
    return {
        "currency": schedule.currency,
        "start_month": schedule.start_month,
        "plans": [payoff(schedule, p.plan_id) for p in schedule.plans],
        "closed_early": closed_early(statements),
    }
