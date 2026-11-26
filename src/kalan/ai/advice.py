"""AI saving suggestions from aggregates only.

The model sees category totals per month, the regular payments (merchant, kind, monthly amount), the frequent
places (merchant, category, monthly average) and the installment load per month. Never a single transaction
row, a card number, a name or an address. Merchant names pass the redactor once more before they go out.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kalan.ai.prompt import LOCALE_NAMES, InvalidModelOutputError, _json_object, money
from kalan.redact import redact_text

Screen = Literal["recurring", "installments", "spending", "statements", "rights", "settings"]
MAX_SUGGESTIONS = 5


class AdviceItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=800)
    screen: Screen | None = None


class AdviceAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    suggestions: list[AdviceItem] = Field(max_length=10)


def build_advice_payload(
    currency: str,
    months: list[dict[str, Any]],
    regular: list[dict[str, Any]],
    frequent: list[dict[str, Any]],
    schedule_months: list[dict[str, Any]],
    open_plans: int,
) -> dict[str, Any]:
    """``months``: monthly summaries (``GET /summary/monthly``); ``schedule_months``: ``[{"month", "total"}]``."""
    return {
        "currency": currency,
        "spend_by_month": [
            {"month": m["month"], "total": money(m["total_spend"]),
             "by_category": {c["category"]: money(c["amount"]) for c in m["by_category"]}}
            for m in months
        ],
        "regular_payments": [
            {"merchant": redact_text(r["merchant"]), "kind": r["kind"], "monthly": money(r["monthly_amount"]),
             "price_change": r["price_change"]}
            for r in regular if r["currency"] == currency
        ],
        "frequent_places": [
            {"merchant": redact_text(f["merchant"]), "category": f["category"],
             "monthly_average": money(f["monthly_average"]), "visits": f["visits"]}
            for f in frequent
        ],
        "installments": {"open_plans": open_plans,
                         "by_month": [{"month": m["month"], "total": money(m["total"])} for m in schedule_months]},
    }


def build_advice_prompt(payload: dict[str, Any], locale: str) -> str:
    language = LOCALE_NAMES.get(locale, "English")
    schema = {"suggestions": [{"title": "short title", "body": "one or two sentences",
                               "screen": "recurring|installments|spending|statements|rights|settings"}]}
    return "\n".join([
        "You help one person spend less on their credit cards. Below are totals computed by code from their "
        "statements (amounts in " + payload["currency"] + ", already summed; there are no single transactions).",
        f"Write up to {MAX_SUGGESTIONS} concrete saving suggestions in {language} ({locale}), addressed to the "
        "person as 'you' (Turkish: 'sen'), plain words, no jargon, no greetings.",
        "Each suggestion says what to do and why, based only on these figures. Do not invent amounts or merchants. "
        "Skip anything obvious from a single number; prefer patterns across months.",
        "'screen' names the app screen where the person can act on it.",
        "",
        "Answer with ONE JSON object only: no prose, no markdown fences. Exact shape:",
        json.dumps(schema, ensure_ascii=False),
        "",
        "Totals:",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ])


SCREEN_PATHS = {"recurring": "/recurring", "installments": "/installments", "spending": "/spending",
                "statements": "/statements", "rights": "/rights", "settings": "/settings"}


def parse_advice(text: str) -> list[dict[str, str]]:
    data = _json_object(text)
    try:
        answer = AdviceAnswer.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise InvalidModelOutputError(f"schema mismatch at {'.'.join(str(p) for p in first['loc'])}: "
                                      f"{first['msg']}") from None
    return [{"title": i.title.strip(), "body": i.body.strip(),
             "link": SCREEN_PATHS.get(i.screen or "", "/")} for i in answer.suggestions[:MAX_SUGGESTIONS]]


__all__ = ["build_advice_payload", "build_advice_prompt", "parse_advice"]
