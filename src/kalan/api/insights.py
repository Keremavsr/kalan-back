"""Routes for the "what is this?" calculations (``/insights/...``). Logic lives in :mod:`kalan.insights`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError
from kalan.api.errors import ApiError
from kalan.insights.card_fee import find_card_fees, placeholders_kept, refund_letter_prompt
from kalan.insights.context import current_schedule, rule_overrides
from kalan.insights.minimum import latest_statement, minimum_payment_plan
from kalan.insights.rules import OVERRIDES_KEY, RuleOverrides, card_rules
from kalan.models import Statement
from kalan.store import Store

router = APIRouter(prefix="/insights", tags=["insights"])


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
