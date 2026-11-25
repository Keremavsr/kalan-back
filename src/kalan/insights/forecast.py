"""Open period and next statement forecast, with a backtest on past statements.

A month here is a statement cut-off month, as in the schedule. Spend is every debit except the opening
balance and payments, in the base currency (the monthly summary rule). The forecast for a month is the
sum of four parts:

* ``installments``: installments already scheduled for that month (known);
* ``subscriptions``: the last amount of every subscription seen in the latest month with data;
* ``fixed``: fixed costs due that month (only with ``include_fixed``; they may not be paid by card);
* ``variable``: the median of the earlier months' other spend (spend minus installment rows, minus
  subscription rows), over at most ``history`` months; at least what is already known for the month.

The backtest replays this on every past month that has enough history, using only data from before
that month, and reports the error against what the statements actually show.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from statistics import median
from typing import Any

from kalan.amounts import add_months, month_key, months_between
from kalan.models import FixedCost, ManualInstallment, Statement, Transaction
from kalan.schedule import build_schedule, fixed_series
from kalan.summary import NOT_SPEND, attributed, subscriptions
from kalan.text import normalize_merchant

DEFAULT_HISTORY = 6
MIN_HISTORY = 2


def _merchant(tx: Transaction) -> str:
    return tx.merchant or normalize_merchant(tx.description)


def _spend_rows(rows: list[tuple[str, Transaction]], currency: str) -> list[tuple[str, Transaction]]:
    return [(m, t) for m, t in rows if t.currency == currency and t.direction == "debit" and t.kind not in NOT_SPEND]


def _split(txs: list[Transaction], subs: set[str]) -> dict[str, int]:
    out = {"installments": 0, "subscriptions": 0, "variable": 0}
    for t in txs:
        if t.installment is not None:
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
) -> dict[str, Any]:
    """Forecast of the spend billed in ``month`` from data attributed to earlier months only
    (loose rows dated in ``month`` count as already known)."""
    past_st = [s for s in statements if months_between(month_key(s.period_end), month) > 0]
    past_loose = [t for t in loose if months_between(month_key(t.date), month) > 0]
    known_loose = [t for t in loose if month_key(t.date) == month]

    sched = build_schedule(past_st, [p for p in manual_plans or [] if month_key(p.start_date) <= month],
                           months=1, start_month=month,
                           imported=[p for p in imported_plans or [] if month_key(p.start_date) <= month])
    installments = sched.months[0].total if sched.months else 0
    installment_items = sched.months[0].items if sched.months else []

    subs = subscriptions(past_st, past_loose, model_recurring)
    sub_names = {s["merchant"] for s in subs if s["currency"] == currency}
    rows = _spend_rows(attributed(past_st, past_loose), currency)
    by_month: dict[str, list[Transaction]] = {}
    for m, t in rows:
        by_month.setdefault(m, []).append(t)
    data_months = sorted(by_month)
    latest = data_months[-1] if data_months else None
    active_subs = [s for s in subs if s["currency"] == currency and latest and s["months_seen"][-1] == latest]
    sub_total = sum(s["amount_last"] for s in active_subs)

    window = [m for m in data_months if months_between(m, month) <= history]
    variable_hist = [_split(by_month[m], sub_names)["variable"] for m in window]
    variable_median = int(Decimal(median(variable_hist)).quantize(Decimal(1), rounding=ROUND_HALF_UP)) \
        if variable_hist else 0
    known_variable = _split([t for _, t in _spend_rows([("", t) for t in known_loose], currency)
                             if t.installment is None], sub_names)["variable"]
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
        "subscriptions": [{"merchant": s["merchant"], "amount": s["amount_last"]} for s in active_subs],
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
                           imported_plans, fixed_costs, model_recurring, include_fixed, history)
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
) -> dict[str, Any]:
    """Open period, forecast of the next statement month and the backtest error next to it."""
    today = today or date.today()
    month = add_months(month_key(max(s.period_end for s in statements)), 1) if statements else month_key(today)
    args = (manual_plans, imported_plans, fixed_costs, model_recurring, include_fixed)
    forecast = forecast_month(month, statements, loose, currency, *args)
    test = backtest(statements, loose, currency, *args)
    return {"open_period": open_period(statements, loose, today), "forecast": forecast,
            "backtest": {k: v for k, v in test.items() if k != "months"}}
