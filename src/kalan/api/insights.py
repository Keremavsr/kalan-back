"""Routes for the "what is this?" calculations (``/insights/...``). Logic lives in :mod:`kalan.insights`."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from kalan.api.errors import ApiError
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
