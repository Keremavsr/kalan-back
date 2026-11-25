"""Installment plans and the month-by-month projection of committed payments.

Months are statement cut-off months (``YYYY-MM`` of ``period_end``): an installment "in 2026-11" is
billed on the statement cut in November.

Rules:

* A plan is identified by ``InstallmentInfo.plan_id`` (parsers make it stable across months).
* The latest statement that contains a plan wins; older rows of the plan are ignored.
* A plan whose latest rows carry ``is_last`` (``Son Taksit``, an early payoff, or no remaining cell)
  is finished and projects nothing.
* For an installment cash advance the amount of one future installment is the sum of all its
  components (principal + BSMV + KKDF + vade farkı) printed in the remaining cells; each item and
  plan carries that split as ``breakdown`` (``null`` for purchases).
* Manual plans (``ManualInstallment``) join the same projection.

Everything here is integer arithmetic on minor units.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from kalan.amounts import add_months, month_key, months_between
from kalan.models import ManualInstallment, Statement, Transaction

DEFAULT_MONTHS = 12


class Breakdown(BaseModel):
    """Parts of a cash advance installment (minor units); they sum to the installment amount."""

    principal: int
    interest: int
    bsmv: int
    kkdf: int

    def total(self) -> int:
        return self.principal + self.interest + self.bsmv + self.kkdf

    def times(self, n: int) -> Breakdown:
        return Breakdown(principal=self.principal * n, interest=self.interest * n, bsmv=self.bsmv * n,
                         kkdf=self.kkdf * n)


class ScheduleItem(BaseModel):
    plan_id: str
    description: str
    amount: int
    index: int | None = None
    count: int | None = None
    breakdown: Breakdown | None = Field(default=None, description="null for plain purchases")


class ScheduleMonth(BaseModel):
    month: str
    total: int
    items: list[ScheduleItem] = Field(default_factory=list)


class PlanSummary(BaseModel):
    plan_id: str
    description: str
    kind: Literal["purchase", "cash_advance"]
    remaining_count: int
    amount_each: int
    ends: str
    total_remaining: int
    source: Literal["statement", "manual", "import"] = "statement"
    breakdown: Breakdown | None = Field(default=None, description="Parts of total_remaining; null for purchases")
    last_statement_id: str | None = None


class Schedule(BaseModel):
    currency: str
    start_month: str
    months: list[ScheduleMonth]
    plans: list[PlanSummary]


@dataclass
class PlanState:
    """State of one plan as of the statement month ``month``."""

    plan_id: str
    description: str
    kind: Literal["purchase", "cash_advance"]
    month: str
    statement_id: str | None
    statement_end: date
    remaining_count: int
    amount_each: int
    index: int | None
    total_count: int | None
    closed: bool
    components: dict[str, int] | None = None
    source: Literal["statement", "manual", "import"] = "statement"


_CA_MARKER = re.compile(r"\s*\d{1,2}\s*\.\s*(Taksit Anap|BSMV|KKDF|Vade Fark\w*)\s*$", re.IGNORECASE)
_SLASH_MARKER = re.compile(r"\s*\(?\b\d{1,2}\s*/\s*\d{1,2}\b\)?")
_DOT_MARKER = re.compile(r"\s*\b\d{1,2}\s*\.\s*Taksit\b", re.IGNORECASE)


def plan_description(description: str) -> str:
    """Printed description without the per-month installment marker (``2/6``, ``1.Taksit Anap``)."""
    text = _CA_MARKER.sub("", description)
    text = _SLASH_MARKER.sub("", text)
    text = _DOT_MARKER.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" -/")


def _state_from_rows(statement: Statement, plan_id: str, rows: list[Transaction]) -> PlanState:
    infos = [t.installment for t in rows if t.installment is not None]
    is_cash = any(i.component is not None for i in infos)
    closed = any(i.is_last for i in infos)
    billed = [(t, t.installment) for t in rows if t.installment and t.installment.component != "early_payoff"]
    indexed = [i.index for _, i in billed if i.index is not None]
    current_index = max(indexed) if indexed else None
    current = [(t, i) for t, i in billed if current_index is None or i.index == current_index] or billed
    remaining = 0 if closed else min(i.remaining_count for _, i in current)
    components: dict[str, int] | None = None
    if is_cash:
        components = {}
        for _, i in current:
            role = i.component or "principal"
            components[role] = components.get(role, 0) + (0 if closed else i.remaining_amount_each)
    amount_each = 0 if closed else sum(i.remaining_amount_each for _, i in current)
    main = next((t for t, i in current if i.component in (None, "principal")), current[0][0] if current else rows[0])
    totals = [i.total_count for _, i in current if i.total_count]
    total_count = max(totals) if totals else (current_index + remaining if current_index else None)
    return PlanState(
        plan_id=plan_id,
        description=plan_description(main.description),
        kind="cash_advance" if is_cash else "purchase",
        month=statement.cut_off_month,
        statement_id=statement.id,
        statement_end=statement.period_end,
        remaining_count=remaining,
        amount_each=amount_each,
        index=current_index,
        total_count=total_count,
        closed=closed,
        components=components,
    )


def _breakdown(s: PlanState) -> Breakdown | None:
    if s.kind != "cash_advance":
        return None
    if not s.components:  # manual cash advance: no split known, all principal
        return Breakdown(principal=s.amount_each, interest=0, bsmv=0, kkdf=0)
    b = Breakdown(
        principal=s.components.get("principal", 0),
        interest=s.components.get("interest", 0),
        bsmv=s.components.get("bsmv", 0),
        kkdf=s.components.get("kkdf", 0),
    )
    assert b.total() == s.amount_each
    return b


def plan_states(statements: list[Statement]) -> dict[str, PlanState]:
    """Latest known state of every plan found in ``statements``."""
    states: dict[str, PlanState] = {}
    for st in sorted(statements, key=lambda s: s.period_end):
        by_plan: dict[str, list[Transaction]] = defaultdict(list)
        for tx in st.transactions:
            if tx.installment is not None:
                by_plan[tx.installment.plan_id].append(tx)
        for plan_id, rows in by_plan.items():
            states[plan_id] = _state_from_rows(st, plan_id, rows)
    return states


def first_billing_month(start: date, cut_off_day: int | None = None) -> str:
    """Statement month of a purchase made on ``start``. With ``cut_off_day``, later days roll over."""
    month = month_key(start)
    if cut_off_day is not None and start.day > cut_off_day:
        month = add_months(month, 1)
    return month


def _manual_state(
    m: ManualInstallment, cut_off_day: int | None, source: Literal["manual", "import"] = "manual"
) -> tuple[PlanState, str]:
    first = first_billing_month(m.start_date, cut_off_day)
    state = PlanState(
        plan_id=m.id,
        description=m.description,
        kind=m.kind,
        month=add_months(first, -1),  # "statement before the first installment"
        statement_id=None,
        statement_end=m.start_date,
        remaining_count=m.total_count,
        amount_each=m.amount_each,
        index=0,
        total_count=m.total_count,
        closed=False,
        source=source,
    )
    return state, first


def build_schedule(
    statements: list[Statement],
    manual: list[ManualInstallment] | None = None,
    months: int = DEFAULT_MONTHS,
    start_month: str | None = None,
    cut_off_day: int | None = None,
    today: date | None = None,
    imported: list[ManualInstallment] | None = None,
) -> Schedule:
    """Project committed installments for ``months`` months starting at ``start_month``.

    ``start_month`` defaults to the month after the latest statement's cut-off (or the current month
    if there are no statements). Plans keep counting beyond the window in ``plans``.
    """
    if months < 1:
        raise ValueError("months must be >= 1")
    states = list(plan_states(statements).values())
    for m in manual or []:
        states.append(_manual_state(m, cut_off_day)[0])
    for m in imported or []:
        states.append(_manual_state(m, cut_off_day, "import")[0])

    if start_month is None:
        if statements:
            start_month = add_months(max(s.period_end for s in statements).strftime("%Y-%m"), 1)
        else:
            start_month = month_key(today or date.today())
    window = [add_months(start_month, i) for i in range(months)]
    month_items: dict[str, list[ScheduleItem]] = {mk: [] for mk in window}
    plans: list[PlanSummary] = []

    for s in sorted(states, key=lambda p: (p.month, p.description, p.plan_id)):
        if s.closed or s.remaining_count <= 0:
            continue
        future: list[tuple[str, int | None]] = []
        for i in range(1, s.remaining_count + 1):
            mk = add_months(s.month, i)
            if months_between(start_month, mk) < 0:
                continue  # already billed on a later statement
            future.append((mk, s.index + i if s.index is not None else None))
        if not future:
            continue
        each_breakdown = _breakdown(s)
        for mk, idx in future:
            if mk in month_items:
                month_items[mk].append(
                    ScheduleItem(
                        plan_id=s.plan_id, description=s.description, amount=s.amount_each, index=idx,
                        count=s.total_count, breakdown=each_breakdown,
                    )
                )
        plans.append(
            PlanSummary(
                plan_id=s.plan_id,
                description=s.description,
                kind=s.kind,
                remaining_count=len(future),
                amount_each=s.amount_each,
                ends=future[-1][0],
                total_remaining=len(future) * s.amount_each,
                source=s.source,
                breakdown=each_breakdown.times(len(future)) if each_breakdown else None,
                last_statement_id=s.statement_id,
            )
        )

    loose = (manual or []) + (imported or [])
    currency = statements[0].currency if statements else (loose[0].currency if loose else "TRY")
    return Schedule(
        currency=currency,
        start_month=start_month,
        months=[ScheduleMonth(month=mk, total=sum(i.amount for i in items), items=items) for mk, items in
                month_items.items()],
        plans=sorted(plans, key=lambda p: (p.ends, p.plan_id)),
    )
