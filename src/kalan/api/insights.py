"""Routes for the "what is this?" calculations (``/insights/...``). Logic lives in :mod:`kalan.insights`."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response

from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError
from kalan.api.errors import ApiError
from kalan.imports import import_plans
from kalan.insights.cancellations import CancelledSubscription, add_cancelled, list_cancelled, remove_cancelled
from kalan.insights.card_fee import find_card_fees, placeholders_kept, refund_letter_prompt
from kalan.insights.context import current_schedule, rule_overrides
from kalan.insights.early_payoff import payoff, payoff_overview
from kalan.insights.export import EXTENSIONS, MEDIA_TYPES, ExportFormat, export
from kalan.insights.fixed_match import fixed_cost_report, matched_transaction_ids
from kalan.insights.forecast import DEFAULT_HISTORY, MIN_HISTORY, backtest, next_statement_forecast
from kalan.insights.minimum import latest_statement, minimum_payment_plan
from kalan.insights.rules import OVERRIDES_KEY, RuleOverrides, card_rules
from kalan.insights.share_card import CardLocale, load_card, load_svg
from kalan.insights.which_card import card_options
from kalan.models import Statement
from kalan.store import Store

router = APIRouter(prefix="/insights", tags=["insights"])
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


def _store(request: Request) -> Store:
    return request.app.state.store


def _statement(store: Store, statement_id: str | None) -> Statement:
    if statement_id is not None:
        st = store.get_statement(statement_id)
        if st is None:
            raise ApiError(404, "statement_not_found", "No statement with this id.", {"id": statement_id})
        return st
    st = latest_statement(store.all_statements())
    if st is None:
        raise ApiError(409, "no_statements", "Upload a statement first.")
    return st


# -- rules ------------------------------------------------------------------------------------------


@router.get("/rules")
def get_rules(request: Request) -> dict[str, Any]:
    return {"rules": card_rules(), "overrides": rule_overrides(_store(request)).model_dump()}


@router.put("/rules")
def put_rules(request: Request, body: RuleOverrides) -> dict[str, Any]:
    store = _store(request)
    store.put_value(OVERRIDES_KEY, body.model_dump(exclude_none=True))
    return {"rules": card_rules(), "overrides": rule_overrides(store).model_dump()}


# -- 1. minimum payment -----------------------------------------------------------------------------


@router.get("/minimum-payment")
def minimum_payment(
    request: Request,
    statement_id: str | None = None,
    include_installments: bool = False,
    cycles: Annotated[int | None, Query(ge=0, le=600)] = None,
) -> dict[str, Any]:
    store = _store(request)
    st = _statement(store, statement_id)
    charges = None
    if include_installments:
        charges = {m.month: m.total for m in current_schedule(store, 60).months}
    result = minimum_payment_plan(st, rule_overrides(store), charges)
    if cycles is not None:
        result["cycles"] = result["cycles"][:cycles]
    return result


# -- 2. card fee refund letter ----------------------------------------------------------------------


def _fee_or_404(store: Store, tx_id: str) -> dict[str, Any]:
    fee = next((f for f in find_card_fees(store.all_statements()) if f["transaction_id"] == tx_id), None)
    if fee is None:
        raise ApiError(404, "card_fee_not_found", "No annual card fee line with this id.", {"id": tx_id})
    return fee


def _selected_adapter(request: Request) -> Adapter:
    store, registry = _store(request), request.app.state.registry
    provider_id = store.selected_provider()
    if provider_id is None:
        raise ApiError(409, "no_provider", "Choose an AI provider first (PUT /providers/selected).")
    adapter = registry.get(provider_id)
    detection = registry.detect_all().get(provider_id)
    if adapter is None or detection is None or not detection.available:
        raise ApiError(409, "provider_unavailable", "The selected provider is not available on this computer.",
                       {"id": provider_id})
    return adapter


@router.get("/card-fees")
def card_fees(request: Request) -> dict[str, Any]:
    return {"items": find_card_fees(_store(request).all_statements())}


@router.get("/card-fees/{tx_id}/prompt")
def card_fee_prompt(request: Request, tx_id: str) -> dict[str, Any]:
    return refund_letter_prompt(_fee_or_404(_store(request), tx_id))


@router.post("/card-fees/{tx_id}/draft")
def card_fee_draft(request: Request, tx_id: str) -> dict[str, Any]:
    built = refund_letter_prompt(_fee_or_404(_store(request), tx_id))
    adapter = _selected_adapter(request)
    try:
        text = adapter.complete(built["prompt"], timeout=DEFAULT_TIMEOUT)
    except AdapterError as exc:
        raise ApiError(502, "provider_failed", f"{adapter.name}: {exc}", {"provider": adapter.id}) from None
    kept = placeholders_kept(text)
    return {"provider": adapter.id, "text": text, "route": built["route"], "placeholders_kept": kept,
            "placeholders_missing": [p for p in built["placeholders"] if p not in kept]}


# -- 3. which card today ----------------------------------------------------------------------------


@router.get("/which-card")
def which_card(request: Request, date: date | None = None) -> dict[str, Any]:
    return card_options(_store(request).all_statements(), today=date)


# -- 4. shareable installment load card -------------------------------------------------------------


@router.get("/share-card")
def share_card(request: Request, locale: CardLocale = "tr", amounts: bool = False) -> dict[str, Any]:
    return load_card(current_schedule(_store(request), 12), locale, amounts)


@router.get("/share-card.svg")
def share_card_svg(request: Request, locale: CardLocale = "tr", amounts: bool = False) -> Response:
    svg = load_svg(current_schedule(_store(request), 12), locale, amounts)
    return Response(svg, media_type="image/svg+xml")


# -- 5. open period and next statement forecast -----------------------------------------------------


def _forecast_inputs(store: Store) -> dict[str, Any]:
    statements, loose, costs = store.all_statements(), store.loose_transactions(), store.fixed_costs()
    return {
        "statements": statements,
        "loose": loose,
        "currency": store.get_settings().base_currency,
        "manual_plans": store.manual_plans(),
        "imported_plans": import_plans(store.import_transactions()),
        "fixed_costs": costs,
        "model_recurring": store.model_recurring_ids(),
        "fixed_tx_ids": matched_transaction_ids(costs, statements, loose),
    }


@router.get("/forecast")
def forecast(request: Request, include_fixed: bool = True) -> dict[str, Any]:
    return next_statement_forecast(**_forecast_inputs(_store(request)), include_fixed=include_fixed)


@router.get("/forecast/backtest")
def forecast_backtest(
    request: Request,
    include_fixed: bool = True,
    history: Annotated[int, Query(ge=1, le=36)] = DEFAULT_HISTORY,
    min_history: Annotated[int, Query(ge=1, le=36)] = MIN_HISTORY,
) -> dict[str, Any]:
    return backtest(**_forecast_inputs(_store(request)), include_fixed=include_fixed, history=history,
                    min_history=min_history)


# -- 6. early payoff --------------------------------------------------------------------------------


@router.get("/early-payoff")
def early_payoff_overview(request: Request) -> dict[str, Any]:
    store = _store(request)
    return payoff_overview(current_schedule(store), store.all_statements())


@router.get("/early-payoff/{plan_id}")
def early_payoff(
    request: Request, plan_id: str, month: Annotated[str | None, Query(pattern=MONTH_PATTERN)] = None
) -> dict[str, Any]:
    sched = current_schedule(_store(request))
    if month is not None and month not in {m.month for m in sched.months}:
        raise ApiError(422, "month_outside_schedule", "The payoff month must be inside the schedule window.",
                       {"start_month": sched.start_month, "months": len(sched.months)})
    result = payoff(sched, plan_id, month)
    if result is None:
        raise ApiError(404, "plan_not_found", "No open installment plan with this id.", {"id": plan_id})
    return result


# -- 7. cancelled subscriptions (alerts) and fixed-cost matching ------------------------------------


@router.get("/cancelled-subscriptions")
def get_cancelled(request: Request) -> dict[str, Any]:
    return {"items": list_cancelled(_store(request))}


@router.post("/cancelled-subscriptions", status_code=201)
def post_cancelled(request: Request, body: CancelledSubscription) -> dict[str, Any]:
    return {"items": add_cancelled(_store(request), body)}


@router.delete("/cancelled-subscriptions", status_code=204)
def delete_cancelled(request: Request, merchant: Annotated[str, Query(min_length=1)]) -> Response:
    if not remove_cancelled(_store(request), merchant):
        raise ApiError(404, "cancellation_not_found", "No cancelled subscription with this merchant.")
    return Response(status_code=204)


@router.get("/fixed-costs/matches")
def fixed_cost_matches(request: Request) -> dict[str, Any]:
    store = _store(request)
    return fixed_cost_report(store.fixed_costs(), store.all_statements(), store.loose_transactions())


# -- 8. export --------------------------------------------------------------------------------------


@router.get("/export")
def export_data(
    request: Request,
    format: ExportFormat,
    what: Literal["transactions", "plans"] = "transactions",
    currency: Annotated[str | None, Query(pattern=r"^[A-Z]{3}$")] = None,
) -> Response:
    store = _store(request)
    if what == "plans" and format != "csv":
        raise ApiError(422, "plans_csv_only", "Plans export on their own as CSV; hledger and beancount include them.")
    sched = current_schedule(store, 60)
    text, skipped = export(format, store.all_statements(), store.loose_transactions(), sched.plans,
                           currency or store.get_settings().base_currency, what)
    name = f"kalan-{what}.{EXTENSIONS[format]}"
    headers = {"Content-Disposition": f'attachment; filename="{name}"', "X-Kalan-Skipped-Rows": str(skipped)}
    return Response(text, media_type=MEDIA_TYPES[format], headers=headers)
