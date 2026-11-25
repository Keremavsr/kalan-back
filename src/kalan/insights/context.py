"""Shared inputs for the insight functions, read from the store the same way the core routes do."""

from __future__ import annotations

from kalan.imports import import_plans
from kalan.insights.rules import OVERRIDES_KEY, RuleOverrides
from kalan.schedule import Schedule, build_schedule
from kalan.store import Store


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
    )
    if not statements and not store.manual_plans() and not imported:
        result.currency = store.get_settings().base_currency
    return result


def rule_overrides(store: Store) -> RuleOverrides:
    raw = store.get_value(OVERRIDES_KEY)
    return RuleOverrides.model_validate(raw) if raw else RuleOverrides()
