"""Alerts computed in code from stored statements, the schedule and subscriptions.

Every alert has a stable id, a ``kind``, a ``severity`` (``info``, ``warning``, ``critical``), the
``month`` it belongs to, a message key plus parameters (the front end translates), and ``refs`` to
the statements, transactions and plans it is about. "This period" means the latest statement of
each card (same bank and last four digits).
"""

from __future__ import annotations

import calendar
import hashlib
from collections import defaultdict
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, Field

from kalan.amounts import add_months, months_between
from kalan.models import Statement
from kalan.schedule import Schedule
from kalan.text import fold, normalize_merchant
from kalan.texts import month_range, text

Severity = Literal["info", "warning", "critical"]
LIMIT_WARNING = 0.80
DUE_SOON_DAYS = 5
OVERDUE_GRACE_DAYS = 7
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
    card_id: str | None = None
    card_label: str | None = None
    text: str | None = Field(default=None, description="Plain sentence in the settings locale, where kalan has one")


def _alert(kind: str, severity: Severity, month: str, params: dict[str, Any], refs: Refs, *key: object,
           card: Statement | None = None) -> Alert:
    digest = hashlib.sha1("|".join(str(p) for p in (kind, month, *key)).encode("utf-8")).hexdigest()[:12]
    return Alert(id=f"al_{digest}", kind=kind, severity=severity, month=month, message_key=f"alert.{kind}",
                 params=params, refs=refs, card_id=card.card_id if card else None,
                 card_label=card.card_label if card else None)


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
    cancelled: list[dict[str, Any]] | None = None,
    locale: str | None = "tr",
) -> list[Alert]:
    """``cancelled``: subscriptions the user marked as cancelled, ``[{"merchant", "cancelled_on"}]``.
    ``subscriptions`` are the regular payments (:func:`kalan.regular.regular_payments`)."""
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
            ).model_copy(update={"card_id": plan.card_id, "card_label": plan.card_label}))

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
                    refs, st.id, card=st,
                ))
        for kind, alert_kind, severity in _CHARGE_KINDS:
            rows = [t for t in st.transactions if t.kind == kind and t.direction == "debit"]
            if rows:
                out.append(_alert(
                    alert_kind, severity, month,  # type: ignore[arg-type]
                    {"count": len(rows), "total": sum(t.amount for t in rows), "currency": st.currency},
                    Refs(statement_ids=[st.id], transaction_ids=[t.id for t in rows]), st.id, card=st,
                ))
        foreign = [t for t in st.transactions if t.original_currency]
        if foreign:
            out.append(_alert(
                "foreign_currency", "info", month,
                {"count": len(foreign), "currencies": sorted({t.original_currency or "" for t in foreign}),
                 "total": sum(t.amount for t in foreign), "currency": st.currency},
                Refs(statement_ids=[st.id], transaction_ids=[t.id for t in foreign]), st.id, card=st,
            ))
        days = (st.due_date - today).days
        if 0 <= days <= DUE_SOON_DAYS and st.statement_total > 0:
            out.append(_alert(
                "due_soon", "critical" if days <= 1 else "warning", month,
                {"due_date": st.due_date.isoformat(), "days_left": days, "statement_total": st.statement_total,
                 "minimum_payment": st.minimum_payment, "currency": st.currency},
                refs, st.id, card=st,
            ))

    missing = _missing_statements(statements, today, locale)
    out += missing

    for sub in subscriptions:
        change = sub.get("price_change")
        if sub.get("kind", "subscription") != "subscription" or sub.get("expected_amount") is not None:
            continue
        if change is not None and float(change) > 0 and sub.get("months_seen"):
            month = sub["months_seen"][-1]
            out.append(_alert(
                "subscription_price_rise", "info", month,
                {"merchant": sub["merchant"], "amount_previous": sub["amount_previous"],
                 "amount_last": sub["amount_last"], "price_change": change, "currency": sub["currency"]},
                Refs(), sub["merchant"],
            ).model_copy(update={"card_id": sub.get("card_id"), "card_label": sub.get("card_label")}))

    covered = {(a.card_id, a.params["bank"], a.params["card_last4"], m) for a in missing
               for m in a.params["month_list"]}
    out += [a for a in _statement_overdue(latest, statements, today)  # the same month as a missing one: once
            if (a.card_id, a.params["bank"], a.params["card_last4"] or None, a.month) not in covered]
    out += _charged_after_cancel(statements, cancelled or [])
    return sorted(out, key=lambda a: (_SEVERITY_ORDER[a.severity], a.month, a.kind, a.id))


def _same_day(month: str, day: int) -> date:
    """``day`` of ``month`` (``YYYY-MM``), the month's last day when it is shorter."""
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


def _next_cut_off(st: Statement) -> date:
    """The cut-off after ``st``: the printed next cut-off, else the same day next month."""
    return st.next_period_end or _same_day(add_months(st.cut_off_month, 1), st.period_end.day)


def _due_months_after(st: Statement, today: date) -> list[str]:
    """Months after a card's latest statement whose cut-off plus grace days has passed by ``today``.

    The first expected cut-off is :func:`_next_cut_off`; later ones fall on its day of each following month. A month
    not cut yet (or cut less than :data:`OVERDUE_GRACE_DAYS` ago) is not missing."""
    first = _next_cut_off(st)
    day = first.day if st.next_period_end else st.period_end.day
    month = max(f"{first.year:04d}-{first.month:02d}", add_months(st.cut_off_month, 1))
    out = []
    cut = first
    while cut + timedelta(days=OVERDUE_GRACE_DAYS) < today:
        out.append(month)
        month = add_months(month, 1)
        cut = _same_day(month, day)
    return out


def _missing_statements(statements: list[Statement], today: date, locale: str | None) -> list[Alert]:
    """One alert per card and gap: between two stored statements, and after the latest one up to ``today``
    (``trailing``), with the missing months as a range."""
    by_card: dict[tuple[str, str], dict[str, Statement]] = defaultdict(dict)
    for st in statements:
        by_card[_card(st)][st.cut_off_month] = st
    out = []
    for (bank, last4), months in sorted(by_card.items()):
        ordered = sorted(months)
        gaps = [([add_months(a, i) for i in range(1, months_between(a, b))], months[a], months[b], False)
                for a, b in pairwise(ordered)]
        latest = months[ordered[-1]]
        gaps.append((_due_months_after(latest, today), latest, latest, True))
        for gap, before, card, trailing in gaps:
            if not gap:
                continue
            label = card.card_label or bank
            key = "alert.missing_statement.one" if len(gap) == 1 else "alert.missing_statement.many"
            ids = [before.id] if trailing else [before.id, card.id]
            alert = _alert(
                "missing_statement", "warning", gap[0],
                {"bank": bank, "card_last4": last4 or None, "card": label, "from": gap[0], "to": gap[-1],
                 "months": len(gap), "month_list": gap, "trailing": trailing},
                Refs(statement_ids=ids), bank, last4, gap[0], card=card,
            )
            out.append(alert.model_copy(update={"text": text(key, locale, card=label,
                                                             range=month_range(gap[0], gap[-1], locale))}))
    return out


def _statement_overdue(latest: list[Statement], statements: list[Statement], today: date) -> list[Alert]:
    """A card's next statement should exist by now (expected cut-off + grace days) but was not uploaded."""
    out = []
    for st in latest:
        nxt = _next_cut_off(st)
        if today > nxt + timedelta(days=OVERDUE_GRACE_DAYS):
            out.append(_alert(
                "statement_overdue", "warning", f"{nxt.year:04d}-{nxt.month:02d}",
                {"bank": st.bank, "card_last4": st.card_last4, "expected_cut_off": nxt.isoformat(),
                 "days_overdue": (today - nxt).days},
                Refs(statement_ids=[st.id]), st.bank, st.card_last4 or "", nxt, card=st,
            ))
    return out


def _charged_after_cancel(statements: list[Statement], cancelled: list[dict[str, Any]]) -> list[Alert]:
    """A debit from a merchant the user marked as cancelled, dated after the cancellation."""
    out = []
    for c in cancelled:
        key = fold(normalize_merchant(c["merchant"]))
        since = date.fromisoformat(c["cancelled_on"])
        if len(key) < 4:
            continue
        for st in statements:
            rows = [t for t in st.transactions if t.direction == "debit" and t.date > since
                    and t.kind in ("purchase", "other")
                    and key in fold(t.merchant or normalize_merchant(t.description))]
            if rows:
                out.append(_alert(
                    "charged_after_cancel", "critical", st.cut_off_month,
                    {"merchant": c["merchant"], "cancelled_on": c["cancelled_on"], "count": len(rows),
                     "total": sum(t.amount for t in rows), "currency": st.currency},
                    Refs(statement_ids=[st.id], transaction_ids=[t.id for t in rows]), st.id, key, card=st,
                ))
    return out


__all__ = ["Alert", "compute_alerts", "latest_per_card"]
