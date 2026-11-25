"""Alerts computed in code from stored statements, the schedule and subscriptions.

Every alert has a stable id, a ``kind``, a ``severity`` (``info``, ``warning``, ``critical``), the
``month`` it belongs to, a message key plus parameters (the front end translates), and ``refs`` to
the statements, transactions and plans it is about. "This period" means the latest statement of
each card (same bank and last four digits).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, Field

from kalan.amounts import add_months, months_between
from kalan.models import Statement
from kalan.schedule import Schedule

Severity = Literal["info", "warning", "critical"]
LIMIT_WARNING = 0.80
DUE_SOON_DAYS = 5
_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_CHARGE_KINDS = (("interest", "interest_charged", "warning"), ("fee", "fee_charged", "info"),
                 ("tax", "tax_charged", "info"))


class Refs(BaseModel):
    statement_ids: list[str] = Field(default_factory=list)
    transaction_ids: list[str] = Field(default_factory=list)
    plan_ids: list[str] = Field(default_factory=list)


class Alert(BaseModel):
    id: str
    kind: str
    severity: Severity
    month: str
    message_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    refs: Refs = Field(default_factory=Refs)


def _alert(kind: str, severity: Severity, month: str, params: dict[str, Any], refs: Refs, *key: object) -> Alert:
    digest = hashlib.sha1("|".join(str(p) for p in (kind, month, *key)).encode("utf-8")).hexdigest()[:12]
    return Alert(id=f"al_{digest}", kind=kind, severity=severity, month=month, message_key=f"alert.{kind}",
                 params=params, refs=refs)


def _card(st: Statement) -> tuple[str, str]:
    return st.bank, st.card_last4 or ""


def latest_per_card(statements: list[Statement]) -> list[Statement]:
    latest: dict[tuple[str, str], Statement] = {}
    for st in statements:
        cur = latest.get(_card(st))
        if cur is None or st.period_end > cur.period_end:
            latest[_card(st)] = st
    return sorted(latest.values(), key=lambda s: (s.period_end, s.id))


def compute_alerts(
    statements: list[Statement],
    schedule: Schedule,
    subscriptions: list[dict[str, Any]],
    today: date | None = None,
) -> list[Alert]:
    today = today or date.today()
    out: list[Alert] = []
    latest = latest_per_card(statements)

    # Installment plans whose last installment falls in the next statement month.
    for plan in schedule.plans:
        if plan.ends == schedule.start_month:
            out.append(_alert(
                "installment_ending", "info", plan.ends,
                {"description": plan.description, "amount_each": plan.amount_each, "currency": schedule.currency,
                 "kind": plan.kind},
                Refs(plan_ids=[plan.plan_id], statement_ids=[plan.last_statement_id] if plan.last_statement_id else []),
                plan.plan_id,
            ))

    for st in latest:
        month = st.cut_off_month
        refs = Refs(statement_ids=[st.id])
        if st.credit_limit:
            used = st.statement_total / st.credit_limit
            if used > LIMIT_WARNING:
                out.append(_alert(
                    "limit_high", "critical" if used >= 1 else "warning", month,
                    {"used_share": f"{used:.2f}", "statement_total": st.statement_total,
                     "credit_limit": st.credit_limit, "currency": st.currency},
                    refs, st.id,
                ))
        for kind, alert_kind, severity in _CHARGE_KINDS:
            rows = [t for t in st.transactions if t.kind == kind and t.direction == "debit"]
            if rows:
                out.append(_alert(
                    alert_kind, severity, month,  # type: ignore[arg-type]
                    {"count": len(rows), "total": sum(t.amount for t in rows), "currency": st.currency},
                    Refs(statement_ids=[st.id], transaction_ids=[t.id for t in rows]), st.id,
                ))
        foreign = [t for t in st.transactions if t.original_currency]
        if foreign:
            out.append(_alert(
                "foreign_currency", "info", month,
                {"count": len(foreign), "currencies": sorted({t.original_currency or "" for t in foreign}),
                 "total": sum(t.amount for t in foreign), "currency": st.currency},
                Refs(statement_ids=[st.id], transaction_ids=[t.id for t in foreign]), st.id,
            ))
        days = (st.due_date - today).days
        if 0 <= days <= DUE_SOON_DAYS and st.statement_total > 0:
            out.append(_alert(
                "due_soon", "critical" if days <= 1 else "warning", month,
                {"due_date": st.due_date.isoformat(), "days_left": days, "statement_total": st.statement_total,
                 "minimum_payment": st.minimum_payment, "currency": st.currency},
                refs, st.id,
            ))

    # Gaps in each card's monthly sequence of statements.
    by_card: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for st in statements:
        by_card[_card(st)][st.cut_off_month] = st.id
    for (bank, last4), months in sorted(by_card.items()):
        seen = sorted(months)
        for a, b in pairwise(seen):
            for gap in range(1, months_between(a, b)):
                missing = add_months(a, gap)
                out.append(_alert(
                    "missing_statement", "warning", missing, {"bank": bank, "card_last4": last4 or None},
                    Refs(statement_ids=[months[a], months[b]]), bank, last4,
                ))

    for sub in subscriptions:
        change = sub.get("price_change")
        if change is not None and float(change) > 0 and sub.get("months_seen"):
            month = sub["months_seen"][-1]
            out.append(_alert(
                "subscription_price_rise", "info", month,
                {"merchant": sub["merchant"], "amount_previous": sub["amount_previous"],
                 "amount_last": sub["amount_last"], "price_change": change, "currency": sub["currency"]},
                Refs(), sub["merchant"],
            ))

    return sorted(out, key=lambda a: (_SEVERITY_ORDER[a.severity], a.month, a.kind, a.id))


__all__ = ["Alert", "compute_alerts", "latest_per_card"]
