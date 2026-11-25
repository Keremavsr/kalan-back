"""What-if: how would a new installment purchase change the coming months? Pure code, no model.

Without interest the amount is split into equal installments; the remainder of the division (a few
minor units) goes to the first installment, as card issuers usually do. With ``interest_monthly``
(percent per month, for example ``"3.11"``) the plan is an annuity: every installment is the same
payment ``P = A * r / (1 - (1 + r) ** -n)``, where ``A`` is the amount, ``r`` the monthly rate as a
fraction and ``n`` the count. Each month's interest is the open balance times ``r``, the rest of the
payment repays principal, and the last installment closes the balance exactly. Example: 1,000.00 in 3
installments at 3% gives three payments of 353.53, total 1,060.59.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from kalan.amounts import add_months, months_between
from kalan.schedule import Schedule


class SimulationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: int = Field(gt=0, description="Purchase amount in minor units")
    count: int = Field(ge=1, le=72)
    start_month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Month of the first installment")
    interest_monthly: str | None = Field(default=None, pattern=r"^\d{1,2}(?:\.\d{1,4})?$",
                                         description="Percent per month, e.g. 3.11")
    months: int | None = Field(default=None, ge=1, le=120, description="Window length; default covers the plan")


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def installments(amount: int, count: int, interest_monthly: str | None) -> list[dict[str, int]]:
    """``[{"amount", "principal", "interest"}]`` for each installment, in minor units."""
    rate = Decimal(interest_monthly) / 100 if interest_monthly else Decimal(0)
    if rate == 0:
        base, rest = divmod(amount, count)
        parts = [base + (rest if i == 0 else 0) for i in range(count)]
        return [{"amount": p, "principal": p, "interest": 0} for p in parts]
    payment = _round(Decimal(amount) * rate / (1 - (1 + rate) ** -count))
    out: list[dict[str, int]] = []
    balance = amount
    for i in range(count):
        interest = _round(Decimal(balance) * rate)
        principal = balance if i == count - 1 else min(payment - interest, balance)
        balance -= principal
        out.append({"amount": principal + interest, "principal": principal, "interest": interest})
    return out


def simulate(body: SimulationIn, baseline: Schedule) -> dict[str, object]:
    plan = installments(body.amount, body.count, body.interest_monthly)
    plan_months = [add_months(body.start_month, i) for i in range(body.count)]
    start = min(baseline.start_month, body.start_month)
    end = plan_months[-1]
    if baseline.months:
        end = max(end, baseline.months[-1].month)
    length = body.months or months_between(start, end) + 1
    window = [add_months(start, i) for i in range(length)]

    without = {m.month: m.total for m in baseline.months}
    extra = dict(zip(plan_months, (p["amount"] for p in plan), strict=True))
    rows = [{"month": mk, "without": without.get(mk, 0), "purchase": extra.get(mk, 0),
             "with": without.get(mk, 0) + extra.get(mk, 0)} for mk in window]

    def peak(key: str) -> dict[str, object] | None:
        if not rows:
            return None
        top = max(enumerate(rows), key=lambda t: (t[1][key], -t[0]))[1]  # earliest month wins a tie
        return {"month": top["month"], "total": top[key]}

    total = sum(p["amount"] for p in plan)
    return {
        "currency": baseline.currency,
        "purchase": {
            "amount": body.amount,
            "count": body.count,
            "start_month": body.start_month,
            "interest_monthly": body.interest_monthly,
            "installments": [{"month": mk, **p} for mk, p in zip(plan_months, plan, strict=True)],
            "total_cost": total,
            "interest_total": total - body.amount,
            "ends": plan_months[-1],
        },
        "months": rows,
        "peak": {"without": peak("without"), "with": peak("with")},
    }
