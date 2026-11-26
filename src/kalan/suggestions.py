"""Saving suggestions computed by code (``GET /suggestions``), plus the AI ones kept from the last batch.

Every amount comes from stored rows, the schedule or the regular payments; the model never supplies a number
shown as a saving. Texts are written in Turkish or English (:mod:`kalan.texts`); each item also carries its
``message_key`` and ``params`` so the front end can write other languages.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from typing import Any
from urllib.parse import quote

from kalan.alerts import latest_per_card
from kalan.models import Statement, Transaction
from kalan.schedule import Schedule
from kalan.summary import NOT_SPEND, attributed
from kalan.text import fold
from kalan.texts import money, month_name, text

AI_KEY = "ai_suggestions"
SERVICE_GROUPS: dict[str, tuple[str, ...]] = {
    "cloud_storage": ("google one", "google storage", "icloud", "dropbox", "onedrive", "yandex disk", "mega", "pcloud",
                      "box"),
    "music": ("spotify", "apple music", "youtube music", "youtube premium", "youtubepremium", "deezer", "fizy",
              "tidal", "amazon music"),
    "video": ("netflix", "disney", "blutv", "hbo max", "max.com", "exxen", "gain", "tabii", "mubi", "prime video",
              "amazon prime", "tod", "beinconnect", "digiturk", "tivibu"),
    "ai_assistant": ("chatgpt", "openai", "claude", "anthropic", "gemini", "perplexity", "midjourney", "copilot"),
}
GROUP_NAMES = {
    "tr": {"cloud_storage": "bulut depolama", "music": "müzik", "video": "dizi ve film",
           "ai_assistant": "yapay zekâ"},
    "en": {"cloud_storage": "cloud storage", "music": "music", "video": "video streaming",
           "ai_assistant": "AI assistant"},
}
DISCRETIONARY = frozenset({"dining", "entertainment", "shopping"})
FREQUENT_SHARE = Decimal("0.05")
FREQUENT_TOP = 3
RELIEF_DROP = Decimal("0.10")
FOREIGN_MONTHS = 3
_LATE = re.compile(r"gecikme|late")


def _id(kind: str, *key: object) -> str:
    return "sg_" + hashlib.sha1("|".join(str(k) for k in (kind, *key)).encode()).hexdigest()[:10]


def _half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _item(kind: str, key: tuple[object, ...], locale: str | None, currency: str, params: dict[str, Any],
          shown: dict[str, Any], link: str, monthly: int | None = None, yearly: int | None = None,
          card: tuple[str | None, str | None] = (None, None)) -> dict[str, Any]:
    return {
        "id": _id(kind, *key),
        "kind": kind,
        "title": text(f"suggestion.{kind}.title", locale, **shown),
        "body": text(f"suggestion.{kind}.body", locale, **shown),
        "monthly_saving": monthly,
        "yearly_saving": yearly,
        "currency": currency,
        "card_id": card[0],
        "card_label": card[1],
        "source": "rules",
        "link": link,
        "message_key": f"suggestion.{kind}",
        "params": params,
    }


def _group(merchant: str) -> str | None:
    name = fold(merchant)
    for group, phrases in SERVICE_GROUPS.items():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", name) for p in phrases):
            return group
    return None


def duplicate_services(regular: list[dict[str, Any]], locale: str | None) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in regular:
        group = _group(item["merchant"]) if item["kind"] == "subscription" else None
        if group:
            groups[(group, item["currency"])].append(item)
    out = []
    for (group, currency), items in sorted(groups.items()):
        if len(items) < 2:
            continue
        items.sort(key=lambda i: (i["monthly_amount"], fold(i["merchant"])))
        cheapest = items[0]
        merchants = [i["merchant"] for i in items]
        saving = cheapest["monthly_amount"]
        out.append(_item(
            "duplicate_service", (group, currency, *sorted(i["id"] for i in items)), locale, currency,
            {"merchants": merchants, "monthly_amount": saving, "group": group,
             "subscription_ids": [i["id"] for i in items]},
            {"count": len(items), "merchants": ", ".join(merchants),
             "group": GROUP_NAMES["tr" if locale == "tr" else "en"][group], "saving": money(saving, currency, locale)},
            f"/recurring?id={cheapest['id']}", monthly=saving, yearly=saving * 12,
        ))
    return out


def price_rises(regular: list[dict[str, Any]], locale: str | None) -> list[dict[str, Any]]:
    out = []
    for item in regular:
        change = item["price_change"]
        if (item["kind"] != "subscription" or item["expected_amount"] is not None or change is None
                or Decimal(change) <= 0):
            continue
        c = item["currency"]
        out.append(_item(
            "price_rise", (item["id"], item["months_seen"][-1]), locale, c,
            {"merchant": item["merchant"], "amount_previous": item["amount_previous"],
             "amount_last": item["amount_last"], "subscription_id": item["id"]},
            {"merchant": item["merchant"], "amount_previous": money(item["amount_previous"], c, locale),
             "amount_last": money(item["amount_last"], c, locale)},
            f"/recurring?id={item['id']}", card=(item["card_id"], item["card_label"]),
        ))
    return out


def card_fee_items(fees: list[dict[str, Any]], statements: list[Statement], locale: str | None) -> list[dict[str, Any]]:
    by_id = {s.id: s for s in statements}
    out = []
    for fee in fees:
        if fee["refunded"]:
            continue
        st = by_id.get(fee["statement_id"])
        label = st.card_label if st else fee["bank"]
        c = fee["currency"]
        out.append(_item(
            "card_fee", (fee["transaction_id"],), locale, c,
            {"amount": fee["amount"], "card": label, "transaction_id": fee["transaction_id"]},
            {"amount": money(fee["amount"], c, locale), "card": label},
            f"/rights?id={fee['transaction_id']}", monthly=_half_up(Decimal(fee["amount"]) / 12), yearly=fee["amount"],
            card=(st.card_id if st else None, label),
        ))
    return out


def _interest_rows(st: Statement) -> list[Transaction]:
    debit = [t for t in st.transactions if t.direction == "debit"]
    interest = [t for t in debit if t.kind == "interest"]
    late = [t for t in debit if t.kind == "fee" and _LATE.search(fold(t.description))]
    taxes = [t for t in debit if t.kind == "tax"] if interest or late else []
    return interest + late + taxes


def interest_items(statements: list[Statement], locale: str | None) -> list[dict[str, Any]]:
    out = []
    for st in latest_per_card(statements):
        rows = _interest_rows(st)
        total = sum(t.amount for t in rows)
        if total <= 0:
            continue
        c = st.currency
        out.append(_item(
            "interest", (st.id,), locale, c,
            {"amount": total, "month": st.cut_off_month, "card": st.card_label, "statement_id": st.id},
            {"amount": money(total, c, locale), "card": st.card_label},
            f"/statements?id={st.id}", monthly=total, card=(st.card_id, st.card_label),
        ))
    return out


def foreign_items(statements: list[Statement], loose: list[Transaction], currency: str,
                  locale: str | None) -> list[dict[str, Any]]:
    rows = attributed(statements, loose)
    months = sorted({m for m, _ in rows})[-FOREIGN_MONTHS:]
    found = [t for m, t in rows if m in months and t.original_currency and t.direction == "debit"
             and t.currency == currency]
    if not found:
        return []
    average = _half_up(Decimal(sum(t.amount for t in found)) / len(months))
    currencies = sorted({t.original_currency or "" for t in found})
    return [_item(
        "foreign_currency", (months[-1],), locale, currency,
        {"monthly_average": average, "currencies": currencies, "months": len(months)},
        {"monthly_average": money(average, currency, locale), "currencies": ", ".join(currencies)},
        "/spending",
    )]


def frequent_items(frequent: dict[str, Any], statements: list[Statement], loose: list[Transaction],
                   locale: str | None) -> list[dict[str, Any]]:
    currency = frequent["currency"]
    window = frequent["window"]["months"]
    if not window:
        return []
    spend = sum(t.amount for m, t in attributed(statements, loose) if m in window and t.currency == currency
                and t.direction == "debit" and t.kind not in NOT_SPEND)
    monthly_spend = Decimal(spend) / len(window)
    out = []
    for place in frequent["items"]:
        if place["category"] not in DISCRETIONARY or monthly_spend <= 0:
            continue
        if Decimal(place["monthly_average"]) < monthly_spend * FREQUENT_SHARE:
            continue
        saving = _half_up(Decimal(place["monthly_average"]) / 2)
        out.append(_item(
            "frequent_place", (fold(place["merchant"]), currency), locale, currency,
            {"merchant": place["merchant"], "monthly_average": place["monthly_average"], "category": place["category"],
             "yearly": saving * 12},
            {"merchant": place["merchant"], "monthly_average": money(place["monthly_average"], currency, locale),
             "yearly": money(saving * 12, currency, locale)},
            f"/spending?merchant={quote(place['merchant'])}", monthly=saving, yearly=saving * 12,
        ))
        if len(out) == FREQUENT_TOP:
            break
    return out


def relief_items(schedule: Schedule, locale: str | None) -> list[dict[str, Any]]:
    months = schedule.months
    for prev, cur in pairwise(months):
        ending = [p for p in schedule.plans if p.ends == prev.month]
        drop = prev.total - cur.total
        if ending and drop > 0 and Decimal(drop) >= Decimal(prev.total) * RELIEF_DROP:
            c = schedule.currency
            return [_item(
                "installment_relief", (cur.month,), locale, c,
                {"month": cur.month, "amount": drop, "plans": len(ending)},
                {"month_name": month_name(cur.month, locale), "amount": money(drop, c, locale), "plans": len(ending)},
                "/installments",
            )]
    return []


def ai_items(stored: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not stored:
        return []
    out = []
    for item in stored.get("items", []):
        out.append({
            "id": _id("ai", stored.get("created_at"), item["title"]),
            "kind": "ai", "title": item["title"], "body": item["body"],
            "monthly_saving": None, "yearly_saving": None, "currency": stored.get("currency"),
            "card_id": None, "card_label": None, "source": "ai", "link": item.get("link") or "/",
            "message_key": None, "params": {},
        })
    return out


def rule_suggestions(
    statements: list[Statement],
    loose: list[Transaction],
    regular: list[dict[str, Any]],
    frequent: dict[str, Any],
    fees: list[dict[str, Any]],
    schedule: Schedule,
    currency: str,
    locale: str | None = "tr",
) -> list[dict[str, Any]]:
    """Every rule-based suggestion, the biggest monthly saving first; the ones without a saving after them."""
    items = (duplicate_services(regular, locale) + price_rises(regular, locale)
             + card_fee_items(fees, statements, locale) + interest_items(statements, locale)
             + foreign_items(statements, loose, currency, locale)
             + frequent_items(frequent, statements, loose, locale) + relief_items(schedule, locale))
    order = {k: i for i, k in enumerate(("duplicate_service", "interest", "card_fee", "frequent_place", "price_rise",
                                         "foreign_currency", "installment_relief"))}
    return sorted(items, key=lambda i: (i["monthly_saving"] is None, -(i["monthly_saving"] or 0), order[i["kind"]],
                                        i["id"]))


__all__ = ["AI_KEY", "SERVICE_GROUPS", "ai_items", "rule_suggestions"]
