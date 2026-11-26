"""Routes of contract v0.4.0: cards, regular payments, frequent places, closing plans, suggestions, AI batch.

The logic lives in :mod:`kalan.cards`, :mod:`kalan.regular`, :mod:`kalan.schedule`, :mod:`kalan.suggestions` and
:mod:`kalan.ai.jobs`; routes only read the store and shape the answer.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from kalan.ai.adapters import Adapter
from kalan.ai.jobs import JobRunner
from kalan.api.errors import ApiError
from kalan.cards import list_cards
from kalan.insights.card_fee import find_card_fees
from kalan.insights.context import (
    current_schedule,
    frequent,
    plan_closures,
    regular,
    set_expected,
    set_plan_closed,
    write_ai_suggestions,
)
from kalan.regular import totals
from kalan.store import Job, Store
from kalan.suggestions import AI_KEY, ai_items, rule_suggestions

router = APIRouter(tags=["overview"])
TextLocale = Literal["tr", "en"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CardName(_Body):
    name: str | None = Field(default=None, max_length=40)


class ExpectedAmount(_Body):
    amount: int = Field(gt=0, description="Minor units")


def _store(request: Request) -> Store:
    return request.app.state.store


def _locale(store: Store, locale: str | None) -> str:
    return locale or store.get_settings().locale


# -- cards ------------------------------------------------------------------------------------------


@router.get("/cards")
def get_cards(request: Request) -> dict[str, Any]:
    store = _store(request)
    return {"items": list_cards(store.all_statements(), store.card_names())}


@router.put("/cards/{card_id}")
def put_card(request: Request, card_id: str, body: CardName) -> dict[str, Any]:
    store = _store(request)
    if not any(c["id"] == card_id for c in list_cards(store.all_statements(), {})):
        raise ApiError(404, "card_not_found", "No card with this id.", {"id": card_id})
    store.set_card_name(card_id, (body.name or "").strip() or None)
    return next(c for c in list_cards(store.all_statements(), store.card_names()) if c["id"] == card_id)


# -- installment plans ------------------------------------------------------------------------------


@router.post("/plans/{plan_id}/close")
def close_plan(request: Request, plan_id: str) -> dict[str, Any]:
    store = _store(request)
    sched = current_schedule(store, 60)
    closed = next((p for p in sched.closed_plans if p.plan_id == plan_id), None)
    if closed is not None:
        if closed.closed == "statement":
            raise ApiError(409, "closed_by_statement", "A newer statement already shows this plan as closed.",
                           {"id": plan_id, "statement_id": closed.statement_id})
        return closed.model_dump(mode="json")
    if not any(p.plan_id == plan_id for p in sched.plans):
        raise ApiError(404, "plan_not_found", "No open installment plan with this id.", {"id": plan_id})
    set_plan_closed(store, plan_id, date.today())
    sched = current_schedule(store, 60)
    return next(p for p in sched.closed_plans if p.plan_id == plan_id).model_dump(mode="json")


@router.delete("/plans/{plan_id}/close", status_code=204)
def reopen_plan(request: Request, plan_id: str) -> Response:
    store = _store(request)
    if plan_id not in plan_closures(store):
        raise ApiError(404, "plan_not_closed", "This plan was not closed by you.", {"id": plan_id})
    set_plan_closed(store, plan_id, None)
    return Response(status_code=204)


# -- regular payments and frequent places -----------------------------------------------------------


@router.get("/subscriptions")
def get_subscriptions(request: Request) -> dict[str, Any]:
    items = regular(_store(request))
    return {"items": items, "totals": totals(items)}


def _subscription_or_404(store: Store, subscription_id: str) -> dict[str, Any]:
    item = next((i for i in regular(store) if i["id"] == subscription_id), None)
    if item is None:
        raise ApiError(404, "subscription_not_found", "No regular payment with this id.", {"id": subscription_id})
    return item


@router.put("/subscriptions/{subscription_id}/expected")
def put_expected(request: Request, subscription_id: str, body: ExpectedAmount) -> dict[str, Any]:
    store = _store(request)
    _subscription_or_404(store, subscription_id)
    set_expected(store, subscription_id, body.amount)
    return _subscription_or_404(store, subscription_id)


@router.delete("/subscriptions/{subscription_id}/expected", status_code=204)
def delete_expected(request: Request, subscription_id: str) -> Response:
    store = _store(request)
    item = _subscription_or_404(store, subscription_id)
    if item["expected_amount"] is None:
        raise ApiError(404, "expected_not_set", "No expected amount is set for this payment.", {"id": subscription_id})
    set_expected(store, subscription_id, None)
    return Response(status_code=204)


@router.get("/insights/frequent-places")
def frequent_places(request: Request, months: Annotated[int, Query(ge=1, le=24)] = 6) -> dict[str, Any]:
    return frequent(_store(request), months)


# -- suggestions ------------------------------------------------------------------------------------


def ai_status(store: Store) -> dict[str, Any]:
    job = store.latest_batch_job()
    stored = store.get_value(AI_KEY)
    status = "none" if job is None else job.status
    if job is not None and job.status == "done" and job.error and not stored:
        status = "failed"
    return {"status": status, "provider": stored.get("provider") if stored else None,
            "created_at": stored.get("created_at") if stored else None, "job_id": job.id if job else None}


@router.get("/suggestions")
def get_suggestions(request: Request, locale: TextLocale | None = None) -> dict[str, Any]:
    store = _store(request)
    loc = _locale(store, locale)
    statements, loose = store.all_statements(), store.loose_transactions()
    items = regular(store)
    rules = rule_suggestions(statements, loose, items, frequent(store, items=items), find_card_fees(statements),
                             current_schedule(store, 12), store.get_settings().base_currency, loc)
    return {"items": rules + ai_items(store.get_value(AI_KEY)), "ai": ai_status(store)}


# -- AI batch ---------------------------------------------------------------------------------------


def start_batch(store: Store, runner: JobRunner, adapter: Adapter, include_uncategorised: bool = True,
                also: list[str] | None = None) -> Job:
    """Start (or join) the AI batch: statements first, then suggestions from aggregates."""

    def advise(a: Adapter, timeout: float) -> None:
        write_ai_suggestions(store, a, timeout)

    return runner.start_batch(adapter, advise, include_uncategorised, also)


__all__ = ["ai_status", "router", "start_batch"]
