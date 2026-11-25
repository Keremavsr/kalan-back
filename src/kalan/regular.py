"""Regular payments (subscriptions and bills) and frequent places. Plain integer arithmetic; no model involved.

A merchant's charges are grouped by statement month (the monthly summary rule). Then:

* **bill**: electricity, water, natural gas, internet, phone (category ``utilities`` / ``telecom`` or a bill
  word in the name), charged in at least two consecutive months. The amount may change freely; up to two
  charges in a month are added up.
* **subscription**: exactly one charge a month in at least two consecutive months, each amount close to the
  month before: within 25 % for a known subscription service (category ``subscriptions`` or ``insurance``),
  within 5 % for anything else that is allowed.
* never regular: groceries, dining, fuel and the other everyday categories in :data:`NEVER_REGULAR`. Those, and
  places whose amount moves, are **frequent places** when they come back often enough.

The user may tell kalan a subscription's new amount (``expected``); it counts in totals and the forecast until a
charge after that day shows the amount.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from kalan.amounts import months_between
from kalan.categories import categorize
from kalan.models import Statement, Transaction
from kalan.summary import attributed, ratio
from kalan.text import fold, normalize_merchant

EXPECTED_KEY = "subscription_expected"
REGULAR_KINDS = frozenset({"purchase", "other"})
BILL_CATEGORIES = frozenset({"utilities", "telecom"})
KNOWN_SUBSCRIPTION = frozenset({"subscriptions", "insurance"})
NEVER_REGULAR = frozenset({
    "groceries", "dining", "fuel", "transport", "shopping", "clothing", "electronics", "home", "travel",
    "cash", "fees", "taxes", "transfers",
})
KNOWN_TOLERANCE = Decimal("0.25")
STEADY_TOLERANCE = Decimal("0.05")
BILL_MAX_PER_MONTH = 2
_BILL_WORDS = re.compile(r"(?<![a-z0-9])(elektrik|dogalgaz|dogal gaz|su idaresi|fatura|faturasi|internet|gsm|"
                         r"telekom|edas|enerji)(?![a-z0-9])")
FREQUENT_MIN_MONTHS = 2
FREQUENT_MIN_VISITS = 3
DEFAULT_WINDOW = 6


def subscription_id(merchant: str, currency: str) -> str:
    return "sb_" + hashlib.sha1(f"{fold(merchant)}|{currency}".encode()).hexdigest()[:10]


def _merchant(tx: Transaction) -> str | None:
    if tx.merchant_unreadable:
        return None
    return tx.merchant or normalize_merchant(tx.description) or None


def _category(txs: list[Transaction]) -> str | None:
    """Most common category of the rows, else what the built-in rules say about the newest row."""
    counted = Counter(t.category for t in txs if t.category)
    if counted:
        return sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    found = categorize(txs[-1])
    return found[0] if found else None


def _within(previous: int, current: int, tolerance: Decimal) -> bool:
    if previous == 0:
        return current == 0
    return abs(Decimal(current - previous)) / Decimal(previous) <= tolerance


def _latest_run(months: list[str], ok: Callable[[str], bool], step: Callable[[str, str], bool]) -> list[str]:
    """Latest run (length >= 2) of consecutive months that pass ``ok`` and ``step(previous, month)``, else []."""
    best: list[str] = []
    run: list[str] = []
    for m in months:
        if not ok(m):
            run = []
            continue
        if run and months_between(run[-1], m) == 1 and step(run[-1], m):
            run.append(m)
        else:
            run = [m]
        if len(run) >= 2:
            best = list(run)
    return best


Charges = dict[tuple[str, str], dict[str, list[Transaction]]]


def _charges(statements: list[Statement], loose: list[Transaction]) -> Charges:
    out: Charges = defaultdict(lambda: defaultdict(list))
    for month, tx in attributed(statements, loose):
        if tx.direction != "debit" or tx.kind not in REGULAR_KINDS or tx.installment is not None:
            continue
        merchant = _merchant(tx)
        if merchant:
            out[(merchant, tx.currency)][month].append(tx)
    return out


def _is_bill(merchant: str, category: str | None) -> bool:
    return category in BILL_CATEGORIES or (category not in NEVER_REGULAR and bool(_BILL_WORDS.search(fold(merchant))))


def regular_payments(
    statements: list[Statement],
    loose: list[Transaction],
    model_recurring: set[str] | None = None,
    expected: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Subscriptions and bills, sorted by kind then merchant. ``expected``: ``{id: {"amount", "since"}}``."""
    flagged = model_recurring or set()
    expected = expected or {}
    items = []
    for (merchant, currency), by_month in _charges(statements, loose).items():
        months = sorted(by_month)
        all_rows = sorted((t for m in months for t in by_month[m]), key=lambda t: t.date)
        category = _category(all_rows)
        totals = {m: sum(t.amount for t in by_month[m]) for m in months}
        if _is_bill(merchant, category):
            kind = "bill"
            run = _latest_run(months, lambda m, bm=by_month: len(bm[m]) <= BILL_MAX_PER_MONTH,
                              lambda a, b: True)
            if len(run) < 2:
                continue
            seen, last, prev = run, totals[run[-1]], totals[run[-2]]
            last_rows = by_month[run[-1]]
        elif category in NEVER_REGULAR:
            continue
        else:
            kind = "subscription"
            tolerance = KNOWN_TOLERANCE if category in KNOWN_SUBSCRIPTION else STEADY_TOLERANCE
            single = {m: by_month[m][0].amount for m in months if len(by_month[m]) == 1}
            run = _latest_run(months, lambda m, one=single: m in one,
                              lambda a, b, one=single, tol=tolerance: _within(one[a], one[b], tol))
            if len(run) >= 2:
                seen, last, prev = run, single[run[-1]], single[run[-2]]
                last_rows = by_month[run[-1]]
            elif any(t.id in flagged for t in all_rows):
                seen = months
                last = all_rows[-1].amount
                prev = all_rows[-2].amount if len(all_rows) > 1 else None
                last_rows = [all_rows[-1]]
            else:
                continue
        sid = subscription_id(merchant, currency)
        latest_tx = max(last_rows, key=lambda t: t.date)
        exp = _active_expected(expected.get(sid), all_rows) if kind == "subscription" else None
        items.append({
            "id": sid,
            "kind": kind,
            "merchant": merchant,
            "category": category,
            "amount_last": last,
            "amount_previous": prev,
            "currency": currency,
            "months_seen": seen,
            "price_change": None if kind == "bill" or prev is None else ratio(last - prev, prev),
            "expected_amount": exp["amount"] if exp else None,
            "expected_since": exp["since"] if exp else None,
            "monthly_amount": exp["amount"] if exp else last,
            "last_date": latest_tx.date.isoformat(),
            "card_id": latest_tx.card_id,
            "card_label": latest_tx.card_label,
        })
    return sorted(items, key=lambda i: (i["kind"] != "subscription", fold(i["merchant"]), i["currency"]))


def expected_matches(amount: int, expected: int) -> bool:
    """A charge shows the expected amount: within 1 %, at least one currency unit (100 minor units)."""
    return abs(amount - expected) <= max(100, expected // 100)


def _active_expected(entry: dict[str, Any] | None, rows: list[Transaction]) -> dict[str, Any] | None:
    if not entry:
        return None
    since = date.fromisoformat(entry["since"])
    if any(t.date > since and expected_matches(t.amount, entry["amount"]) for t in rows):
        return None  # a newer charge shows the new amount: the flag clears itself
    return entry


def stale_expected(items: list[dict[str, Any]], expected: dict[str, dict[str, Any]]) -> list[str]:
    """Ids of stored expected amounts that no longer apply (cleared by a charge, or no such item)."""
    active = {i["id"] for i in items if i["expected_amount"] is not None}
    return [k for k in expected if k not in active]


def totals(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for i in items:
        row = out.setdefault(i["currency"], {"currency": i["currency"], "subscription": 0, "bill": 0})
        row[i["kind"]] += i["monthly_amount"]
    return [out[c] for c in sorted(out)]


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def window_months(statements: list[Statement], loose: list[Transaction], months: int = DEFAULT_WINDOW) -> list[str]:
    seen = sorted({m for m, _ in attributed(statements, loose)})
    return seen[-months:] if months > 0 else []


def frequent_places(
    statements: list[Statement],
    loose: list[Transaction],
    currency: str,
    regular: list[dict[str, Any]] | None = None,
    months: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """Places charged often that are no subscription or bill (see the module docstring)."""
    window = window_months(statements, loose, months)
    regular_keys = {(fold(i["merchant"]), i["currency"]) for i in regular or []}
    rows: dict[str, list[tuple[str, Transaction]]] = defaultdict(list)
    names: dict[str, str] = {}
    for month, tx in attributed(statements, loose):
        if (month not in window or tx.direction != "debit" or tx.kind != "purchase" or tx.installment is not None
                or tx.currency != currency):
            continue
        merchant = _merchant(tx)
        if not merchant or (fold(merchant), currency) in regular_keys:
            continue
        key = fold(merchant)
        names.setdefault(key, merchant)
        rows[key].append((month, tx))
    items = []
    count = len(window)
    for key, found in rows.items():
        seen = {m for m, _ in found}
        if len(seen) < FREQUENT_MIN_MONTHS or len(found) < FREQUENT_MIN_VISITS:
            continue
        txs = sorted((t for _, t in found), key=lambda t: t.date)
        total = sum(t.amount for t in txs)
        items.append({
            "merchant": names[key],
            "kind": "frequent_place",
            "category": _category(txs),
            "months_seen": len(seen),
            "visits": len(txs),
            "total": total,
            "monthly_average": _round(Decimal(total) / count),
            "last_date": txs[-1].date.isoformat(),
            "card_ids": sorted({t.card_id for t in txs if t.card_id}),
        })
    items.sort(key=lambda i: (-i["monthly_average"], fold(i["merchant"])))
    return {"currency": currency, "window": {"months": window, "count": count}, "items": items}


__all__ = ["EXPECTED_KEY", "frequent_places", "regular_payments", "stale_expected", "subscription_id", "totals"]
