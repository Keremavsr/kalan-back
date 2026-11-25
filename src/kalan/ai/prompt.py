"""What the model sees and what it must answer.

The payload holds only, per transaction, the id, date, description, amount, currency and kind, plus
totals computed here. Card number, card digits, name, address, IBAN and customer numbers are never
part of it; descriptions are passed through the redactor once more as a second line of defence.
The id is an opaque hash (``tx_…``) the answer needs to point back at a transaction.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kalan.models import Statement
from kalan.redact import redact_text

Category = Literal[
    "groceries",
    "dining",
    "transport",
    "fuel",
    "shopping",
    "clothing",
    "electronics",
    "home",
    "utilities",
    "telecom",
    "health",
    "education",
    "entertainment",
    "subscriptions",
    "travel",
    "insurance",
    "cash",
    "fees",
    "taxes",
    "transfers",
    "other",
]
CATEGORIES: tuple[str, ...] = get_args(Category)
LOCALE_NAMES = {"tr": "Turkish", "en": "English"}


class InvalidModelOutputError(ValueError):
    """The model's answer is not the JSON object the prompt asked for."""


class WatchItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = Field(min_length=1, max_length=200)
    detail: str = Field(max_length=2000)
    transaction_ids: list[str] = Field(default_factory=list)


class Commentary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(min_length=1, max_length=4000)
    watch_items: list[WatchItem] = Field(default_factory=list, max_length=20)


class ModelAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    categories: dict[str, Category]
    commentary: Commentary
    recurring: list[str] = Field(default_factory=list)


def money(minor: int) -> str:
    """Minor units to a plain decimal string (``-12.30``); two decimal places."""
    sign = "-" if minor < 0 else ""
    minor = abs(minor)
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def build_payload(statement: Statement) -> dict[str, Any]:
    by_kind: dict[str, int] = defaultdict(int)
    debits = credits = 0
    rows = []
    for tx in statement.transactions:
        by_kind[tx.kind] += tx.signed_amount
        if tx.direction == "debit":
            debits += tx.amount
        else:
            credits += tx.amount
        rows.append({
            "id": tx.id,
            "date": tx.date.isoformat(),
            "description": redact_text(tx.description),
            "amount": money(tx.signed_amount),
            "currency": tx.currency,
            "kind": tx.kind,
        })
    return {
        "totals": {
            "currency": statement.currency,
            "period_end": statement.period_end.isoformat(),
            "due_date": statement.due_date.isoformat(),
            "statement_total": money(statement.statement_total),
            "minimum_payment": money(statement.minimum_payment),
            "debits": money(debits),
            "credits": money(credits),
            "by_kind": {k: money(v) for k, v in sorted(by_kind.items())},
            "transaction_count": len(rows),
        },
        "transactions": rows,
    }


def build_prompt(payload: dict[str, Any], locale: str) -> str:
    language = LOCALE_NAMES.get(locale, "English")
    schema = {
        "categories": {"<transaction id>": "<one category from the list>"},
        "commentary": {
            "summary": "<2-4 sentences>",
            "watch_items": [{"title": "<short>", "detail": "<1-2 sentences>", "transaction_ids": ["<id>"]}],
        },
        "recurring": ["<id of a transaction that looks like a recurring subscription>"],
    }
    return "\n".join([
        "You review one credit card statement for its owner.",
        "All totals below were computed by code and are correct; do not recompute or correct them.",
        "Amounts are decimal strings in the given currency; negative amounts are credits.",
        "",
        "Tasks:",
        "1. Give every transaction exactly one category from this list: " + ", ".join(CATEGORIES) + ".",
        f"2. Write a short commentary in {language} ({locale}): a summary and up to 5 watch items "
        "(unusual charges, fees or interest, cash advances, price increases). Refer to transactions by id.",
        "3. List the ids of transactions that look like recurring subscriptions.",
        "",
        "Answer with ONE JSON object only: no prose, no markdown fences. Exact shape:",
        json.dumps(schema, ensure_ascii=False),
        "",
        "Statement data:",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ])


def retry_prompt(prompt: str, error: str) -> str:
    return (
        prompt
        + "\n\nYour previous answer could not be used: "
        + error[:300]
        + "\nAnswer again with ONE JSON object of exactly the shape above and nothing else."
    )


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _json_object(text: str) -> Any:
    text = _FENCE.sub("", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise InvalidModelOutputError("the answer contains no JSON object") from None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise InvalidModelOutputError(f"invalid JSON: {exc.msg}") from None


def parse_answer(text: str, known_ids: set[str]) -> ModelAnswer:
    """Validate the model's text. Ids the statement does not contain are dropped."""
    data = _json_object(text)
    try:
        answer = ModelAnswer.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise InvalidModelOutputError(f"schema mismatch at {where}: {first['msg']}") from None
    categories = {k: v for k, v in answer.categories.items() if k in known_ids}
    if known_ids and not categories:
        raise InvalidModelOutputError("no transaction id of the statement was categorised")
    for item in answer.commentary.watch_items:
        item.transaction_ids = [i for i in item.transaction_ids if i in known_ids]
    return answer.model_copy(update={
        "categories": categories,
        "recurring": [i for i in answer.recurring if i in known_ids],
    })
