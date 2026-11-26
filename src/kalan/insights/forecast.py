"""Open period and next statement forecast, with a backtest on past statements.

A month here is a statement cut-off month, as in the schedule. Spend is every debit except the opening
balance and payments, in the base currency (the monthly summary rule). The forecast for a month is the
sum of four parts:

* ``installments``: installments already scheduled for that month (known);
* ``subscriptions``: the last amount of every subscription seen in the latest month with data;
* ``fixed``: fixed costs due that month (only with ``include_fixed``; they may not be paid by card);
* ``variable``: the median of the earlier months' other spend (spend minus installment rows, minus
  subscription rows), over at most ``history`` months; at least what is already known for the month.
  With ``include_fixed``, rows matched to a fixed cost (``fixed_tx_ids``) are left out of it, so a
  fixed cost paid by card is not counted twice.

The backtest replays this on every past month that has enough history, using only data from before
that month, and reports the error against what the statements actually show.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Any

from kalan.amounts import add_months, month_key, months_between
from kalan.cards import card_id
from kalan.models import FixedCost, ManualInstallment, Statement, Transaction
from kalan.regular import regular_payments
from kalan.schedule import build_schedule, fixed_series
from kalan.summary import NOT_SPEND, attributed
from kalan.text import normalize_merchant
from kalan.texts import text

DEFAULT_HISTORY = 6
MIN_HISTORY = 2


def _merchant(tx: Transaction) -> str:
    return tx.merchant or normalize_merchant(tx.description)


def _spend_rows(rows: list[tuple[str, Transaction]], currency: str) -> list[tuple[str, Transaction]]:
    return [(m, t) for m, t in rows if t.currency == currency and t.direction == "debit" and t.kind not in NOT_SPEND]


def _split(txs: list[Transaction], subs: set[str], fixed_ids: frozenset[str] = frozenset()) -> dict[str, int]:
    out = {"installments": 0, "subscriptions": 0, "variable": 0, "fixed": 0}
    for t in txs:
        if t.id in fixed_ids:
            out["fixed"] += t.amount
        elif t.installment is not None:
            out["installments"] += t.amount
        elif _merchant(t) in subs:
            out["subscriptions"] += t.amount
        else:
            out["variable"] += t.amount
    return out


def forecast_month(
    month: str,
    statements: list[Statement],
    loose: list[Transaction],
    currency: str,
    manual_plans: list[ManualInstallment] | None = None,
    imported_plans: list[ManualInstallment] | None = None,
    fixed_costs: list[FixedCost] | None = None,
    model_recurring: set[str] | None = None,
    include_fixed: bool = True,
    history: int = DEFAULT_HISTORY,
    fixed_tx_ids: set[str] | None = None,
    closed_by_user: dict[str, date] | None = None,
    expected: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Forecast of the spend billed in ``month`` from data attributed to earlier months only
    (loose rows dated in ``month`` count as already known). ``closed_by_user`` leaves those plans out;
    ``expected`` holds the user's new subscription amounts."""
    past_st = [s for s in statements if months_between(month_key(s.period_end), month) > 0]
    past_loose = [t for t in loose if months_between(month_key(t.date), month) > 0]
    known_loose = [t for t in loose if month_key(t.date) == month]

    sched = build_schedule(past_st, [p for p in manual_plans or [] if month_key(p.start_date) <= month],
                           months=1, start_month=month,
                           imported=[p for p in imported_plans or [] if month_key(p.start_date) <= month],
                           closed_by_user=closed_by_user)
    installments = sched.months[0].total if sched.months else 0
    installment_items = sched.months[0].items if sched.months else []

    subs = regular_payments(past_st, past_loose, model_recurring, expected)
    sub_names = {s["merchant"] for s in subs if s["currency"] == currency}
    rows = _spend_rows(attributed(past_st, past_loose), currency)
    by_month: dict[str, list[Transaction]] = {}
    for m, t in rows:
        by_month.setdefault(m, []).append(t)
    data_months = sorted(by_month)
    latest = data_months[-1] if data_months else None
    active_subs = [s for s in subs if s["currency"] == currency and latest and s["months_seen"][-1] == latest]
    sub_total = sum(s["monthly_amount"] for s in active_subs)

    window = [m for m in data_months if months_between(m, month) <= history]
    fixed_ids = frozenset(fixed_tx_ids or ()) if include_fixed else frozenset()
    variable_hist = [_split(by_month[m], sub_names, fixed_ids)["variable"] for m in window]
    variable_median = int(Decimal(median(variable_hist)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) \
        if variable_hist else 0
    known_variable = _split([t for _, t in _spend_rows([("", t) for t in known_loose], currency)
                             if t.installment is None], sub_names, fixed_ids)["variable"]
    variable = max(variable_median, known_variable)

    fixed = fixed_series(fixed_costs or [], [month], currency).months[0].total if fixed_costs else 0
    total = installments + sub_total + variable + (fixed if include_fixed else 0)
    return {
        "month": month,
        "currency": currency,
        "total": total,
        "parts": {"installments": installments, "subscriptions": sub_total, "variable": variable, "fixed": fixed},
        "include_fixed": include_fixed,
        "variable_basis": {"months": window, "median": variable_median, "known_in_month": known_variable},
        "installment_items": [i.model_dump(mode="json") for i in installment_items],
        "subscriptions": [{"id": s["id"], "merchant": s["merchant"], "amount": s["monthly_amount"]}
                          for s in active_subs],
    }


def actual_spend(month: str, statements: list[Statement], loose: list[Transaction], currency: str) -> int:
    rows = _spend_rows(attributed(statements, loose), currency)
    return sum(t.amount for m, t in rows if m == month)


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def backtest(
    statements: list[Statement],
    loose: list[Transaction],
    currency: str,
    manual_plans: list[ManualInstallment] | None = None,
    imported_plans: list[ManualInstallment] | None = None,
    fixed_costs: list[FixedCost] | None = None,
    model_recurring: set[str] | None = None,
    include_fixed: bool = True,
    history: int = DEFAULT_HISTORY,
    min_history: int = MIN_HISTORY,
    fixed_tx_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Forecast every past statement month that has ``min_history`` earlier months of statements, and compare.

    ``mae`` is the mean absolute error in minor units; ``mape`` the mean of ``|error| / actual`` over
    months with spend; ``bias`` the mean signed error (positive: the forecast was too high).
    """
    months = sorted({month_key(s.period_end) for s in statements})
    rows = []
    for i, m in enumerate(months):
        if i < min_history:
            continue
        f = forecast_month(m, statements, [t for t in loose if month_key(t.date) < m], currency, manual_plans,
                           imported_plans, fixed_costs, model_recurring, include_fixed, history, fixed_tx_ids)
        actual = actual_spend(m, statements, loose, currency)
        rows.append({"month": m, "forecast": f["total"], "actual": actual, "error": f["total"] - actual,
                     "parts": f["parts"]})
    if not rows:
        return {"currency": currency, "months": [], "count": 0, "mae": None, "mape": None, "bias": None,
                "min_history": min_history}
    errors = [r["error"] for r in rows]
    with_spend = [abs(r["error"]) / Decimal(r["actual"]) for r in rows if r["actual"] > 0]
    return {
        "currency": currency,
        "months": rows,
        "count": len(rows),
        "mae": round(sum(abs(e) for e in errors) / len(errors)),
        "mape": _ratio(sum(with_spend, Decimal(0)) / len(with_spend)) if with_spend else None,
        "bias": round(sum(errors) / len(errors)),
        "min_history": min_history,
    }


def open_period(statements: list[Statement], loose: list[Transaction], today: date | None = None) -> dict[str, Any]:
    """What is known since the latest cut-off: loose (manual, imported) rows dated after it."""
    today = today or date.today()
    if not statements:
        return {"from": None, "to": today.isoformat(), "transactions": [], "debit_total": 0, "credit_total": 0}
    last = max(s.period_end for s in statements)
    rows = [t for t in loose if t.date > last]
    return {
        "from": (last + timedelta(days=1)).isoformat(),
        "to": today.isoformat(),
        "transactions": [t.model_dump(mode="json") for t in rows],
        "debit_total": sum(t.amount for t in rows if t.direction == "debit"),
        "credit_total": sum(t.amount for t in rows if t.direction == "credit"),
    }


def next_statement_forecast(
    statements: list[Statement],
    loose: list[Transaction],
    currency: str,
    manual_plans: list[ManualInstallment] | None = None,
    imported_plans: list[ManualInstallment] | None = None,
    fixed_costs: list[FixedCost] | None = None,
    model_recurring: set[str] | None = None,
    include_fixed: bool = True,
    today: date | None = None,
    fixed_tx_ids: set[str] | None = None,
    closed_by_user: dict[str, date] | None = None,
    expected: dict[str, dict[str, Any]] | None = None,
    locale: str | None = "tr",
) -> dict[str, Any]:
    """Open period, forecast of the next statement month and the backtest error next to it, then one row per
    card with what to pay next (:func:`card_rows`)."""
    today = today or date.today()
    month = add_months(month_key(max(s.period_end for s in statements)), 1) if statements else month_key(today)
    args = (manual_plans, imported_plans, fixed_costs, model_recurring, include_fixed)
    forecast = forecast_month(month, statements, loose, currency, *args, fixed_tx_ids=fixed_tx_ids,
                              closed_by_user=closed_by_user, expected=expected)
    test = backtest(statements, loose, currency, *args, fixed_tx_ids=fixed_tx_ids)
    regular = regular_payments(statements, loose, model_recurring, expected)
    rows = card_rows(statements, regular, currency, closed_by_user, today, locale)
    return {"open_period": open_period(statements, loose, today), "forecast": forecast,
            "backtest": {k: v for k, v in test.items() if k != "months"},
            "currency": currency,
            "total": sum(r["amount"] for r in rows if r["currency"] == currency),
            "fixed": forecast["parts"]["fixed"],
            "cards": rows}


def _next_dates(st: Statement) -> tuple[date, date, str]:
    """Next cut-off and due date of a card: printed on its latest statement, else one month later."""
    if st.next_period_end is not None and st.next_due_date is not None:
        return st.next_period_end, st.next_due_date, "printed"
    y, m = (st.period_end.year, st.period_end.month + 1) if st.period_end.month < 12 else (st.period_end.year + 1, 1)
    cut = st.next_period_end or date(y, m, min(st.period_end.day, calendar.monthrange(y, m)[1]))
    return cut, cut + (st.due_date - st.period_end), "estimated"


def card_statement_forecast(
    card_statements: list[Statement],
    regular: list[dict[str, Any]],
    currency: str,
    closed_by_user: dict[str, date] | None = None,
    locale: str | None = "tr",
    history: int = DEFAULT_HISTORY,
) -> dict[str, Any]:
    """Next statement of one card: its open installments that month, the regular payments last charged on it and
    what it usually spent on everything else (middle value of up to ``history`` earlier statements)."""
    sts = sorted(card_statements, key=lambda s: (s.period_end, s.id))
    latest = sts[-1]
    cid = card_id(latest.bank, latest.card_last4)
    month = add_months(latest.cut_off_month, 1)
    cut_off, due, basis = _next_dates(latest)
    sched = build_schedule(sts, months=1, start_month=month, closed_by_user=closed_by_user)
    items = sched.months[0].items
    installments = sched.months[0].total
    mine = [r for r in regular if r["card_id"] == cid and r["currency"] == currency
            and r["months_seen"] and r["months_seen"][-1] == latest.cut_off_month]
    subs = sum(r["monthly_amount"] for r in mine)
    names = {r["merchant"] for r in mine} | {r["merchant"] for r in regular if r["card_id"] == cid}
    past = sts[-history:]
    other_hist = []
    for st in past:
        rows = [t for _, t in _spend_rows([("", t) for t in st.transactions], currency)]
        other_hist.append(_split(rows, names)["variable"])
    other = int(Decimal(median(other_hist)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if other_hist else 0
    n = len(other_hist)
    key = "forecast.note.no_history" if n == 0 else "forecast.note.one_month" if n == 1 else "forecast.note.usual"
    return {
        "month": month,
        "cut_off": cut_off.isoformat(),
        "due_date": due.isoformat(),
        "dates": basis,
        "total": installments + subs + other,
        "parts": {"installments": installments, "subscriptions": subs, "other": other},
        "installment_items": [i.model_dump(mode="json") for i in items],
        "subscriptions": [{"id": r["id"], "merchant": r["merchant"], "amount": r["monthly_amount"]} for r in mine],
        "note": text(key, locale, months=n),
        "note_key": key,
        "note_params": {"months": n},
    }


def card_rows(
    statements: list[Statement],
    regular: list[dict[str, Any]],
    currency: str,
    closed_by_user: dict[str, date] | None = None,
    today: date | None = None,
    locale: str | None = "tr",
) -> list[dict[str, Any]]:
    """One row per card: the next payment due. While the latest statement's due date has not passed and it owes
    something, that statement (``estimated: false``); otherwise the forecast of the next statement."""
    today = today or date.today()
    groups: dict[str, list[Statement]] = {}
    for st in statements:
        groups.setdefault(card_id(st.bank, st.card_last4), []).append(st)
    rows = []
    for cid, sts in groups.items():
        latest = max(sts, key=lambda s: (s.period_end, s.id))
        nxt = card_statement_forecast(sts, regular, latest.currency, closed_by_user, locale)
        if latest.due_date >= today and latest.statement_total > 0:
            row = {"due_date": latest.due_date.isoformat(), "amount": latest.statement_total, "estimated": False,
                   "statement_id": latest.id, "minimum_payment": latest.minimum_payment, "parts": None}
        else:
            row = {"due_date": nxt["due_date"], "amount": nxt["total"], "estimated": True, "statement_id": None,
                   "minimum_payment": None, "parts": nxt["parts"]}
        row["days_left"] = (date.fromisoformat(row["due_date"]) - today).days
        rows.append({"card_id": cid, "card_label": latest.card_label, "bank": latest.bank,
                     "card_last4": latest.card_last4, "currency": latest.currency, **row, "next_statement": nxt})
    return sorted(rows, key=lambda r: (r["due_date"], r["card_label"] or "", r["card_id"]))
