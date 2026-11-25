"""Subscriptions the user marked as cancelled (for the ``charged_after_cancel`` alert).

Stored as one JSON value in the settings table; the merchant name is redacted before it is stored.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kalan.redact import redact_text
from kalan.store import Store
from kalan.text import fold

CANCELLED_KEY = "insights.cancelled_subscriptions"


class CancelledSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")
    merchant: str = Field(min_length=4, max_length=120, description="As shown by GET /subscriptions")
    cancelled_on: date


def list_cancelled(store: Store) -> list[dict[str, Any]]:
    return list(store.get_value(CANCELLED_KEY) or [])


def add_cancelled(store: Store, item: CancelledSubscription) -> list[dict[str, Any]]:
    """Add or replace (same merchant, ignoring case and accents) a cancellation."""
    merchant = redact_text(item.merchant)
    items = [c for c in list_cancelled(store) if fold(c["merchant"]) != fold(merchant)]
    items.append({"merchant": merchant, "cancelled_on": item.cancelled_on.isoformat()})
    items.sort(key=lambda c: (c["cancelled_on"], c["merchant"]))
    store.put_value(CANCELLED_KEY, items)
    return items


def remove_cancelled(store: Store, merchant: str) -> bool:
    items = list_cancelled(store)
    kept = [c for c in items if fold(c["merchant"]) != fold(merchant)]
    store.put_value(CANCELLED_KEY, kept)
    return len(kept) != len(items)
