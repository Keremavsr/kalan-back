"""Shared inputs for the insight functions, read from the store the same way the core routes do.

Every route, the CLI and the MCP server get the schedule, regular payments and frequent places from here, so
closed plans, card labels and expected subscription amounts apply everywhere the same way.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from kalan.ai.adapters import Adapter
from kalan.ai.advice import build_advice_payload, build_advice_prompt, parse_advice
from kalan.ai.prompt import InvalidModelOutputError, retry_prompt
from kalan.imports import import_plans
from kalan.insights.rules import OVERRIDES_KEY, RuleOverrides
from kalan.regular import EXPECTED_KEY, frequent_places, regular_payments, window_months
from kalan.schedule import Schedule, build_schedule
from kalan.store import Store, now_iso
from kalan.suggestions import AI_KEY
from kalan.summary import monthly_summary

CLOSURES_KEY = "plan_closures"


def plan_closures(store: Store) -> dict[str, date]:
    """Plans the user marked as closed: ``{plan_id: day}``."""
    raw = store.get_value(CLOSURES_KEY) or {}
    return {k: date.fromisoformat(v) for k, v in raw.items()}


def set_plan_closed(store: Store, plan_id: str, on: date | None) -> None:
    raw = store.get_value(CLOSURES_KEY) or {}
    if on is None:
        raw.pop(plan_id, None)
    else:
        raw[plan_id] = on.isoformat()
    store.put_value(CLOSURES_KEY, raw)


def current_schedule(store: Store, months: int | None = None) -> Schedule:
    """Same projection as ``GET /installments/schedule``."""
    statements = store.all_statements()
    cut_off_day = max(statements, key=lambda s: s.period_end).period_end.day if statements else None
    imported = import_plans(store.import_transactions())
    result = build_schedule(
        statements,
        store.manual_plans(),
        months=months or store.get_settings().months_ahead,
        cut_off_day=cut_off_day,
        imported=imported,
        fixed_costs=store.fixed_costs(),
        closed_by_user=plan_closures(store),
    )
    if not statements and not store.manual_plans() and not imported:
        result.currency = store.get_settings().base_currency
    return result.with_labels(store.card_labels())


def expected_amounts(store: Store) -> dict[str, dict[str, Any]]:
    return store.get_value(EXPECTED_KEY) or {}


def set_expected(store: Store, subscription_id: str, amount: int | None, since: date | None = None) -> None:
    raw = expected_amounts(store)
    if amount is None:
        raw.pop(subscription_id, None)
    else:
        raw[subscription_id] = {"amount": amount, "since": (since or date.today()).isoformat()}
    store.put_value(EXPECTED_KEY, raw)


def regular(store: Store) -> list[dict[str, Any]]:
    """Subscriptions and bills with the user's expected amounts; expected amounts that cleared are dropped."""
    expected = expected_amounts(store)
    cleared: list[str] = []
    items = regular_payments(store.all_statements(), store.loose_transactions(), store.model_recurring_ids(),
                             expected, cleared)
    if cleared:
        store.put_value(EXPECTED_KEY, {k: v for k, v in expected.items() if k not in cleared})
    return items


def frequent(store: Store, months: int = 6, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return frequent_places(store.all_statements(), store.loose_transactions(), store.get_settings().base_currency,
                           items if items is not None else regular(store), months)


def rule_overrides(store: Store) -> RuleOverrides:
    raw = store.get_value(OVERRIDES_KEY)
    return RuleOverrides.model_validate(raw) if raw else RuleOverrides()


def write_ai_suggestions(store: Store, adapter: Adapter, timeout: float) -> None:
    """Ask the model for saving suggestions from aggregates (:mod:`kalan.ai.advice`) and keep them.

    Raises :class:`AdapterError` or :class:`InvalidModelOutputError` (after one retry); the old ones stay then.
    """
    statements, loose = store.all_statements(), store.loose_transactions()
    settings = store.get_settings()
    currency = settings.base_currency
    items = regular(store)
    window = window_months(statements, loose)
    months = [monthly_summary(statements, loose, m, currency) for m in window]
    sched = current_schedule(store, 12)
    payload = build_advice_payload(
        currency, months, items, frequent(store, items=items)["items"],
        [{"month": m.month, "total": m.total} for m in sched.months], sched.summary.plan_count,
    )
    prompt = build_advice_prompt(payload, settings.locale)
    for attempt in (1, 2):
        answer = adapter.complete(prompt, timeout)
        try:
            found = parse_advice(answer)
            break
        except InvalidModelOutputError as exc:
            if attempt == 2:
                raise
            prompt = retry_prompt(prompt, str(exc))
    store.put_value(AI_KEY, {"provider": adapter.id, "created_at": now_iso(), "locale": settings.locale,
                             "currency": currency, "items": found})
