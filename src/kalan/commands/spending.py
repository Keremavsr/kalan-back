"""``kalan ay`` (monthly summary), ``kalan abonelikler`` (subscriptions), ``kalan islemler`` (transactions)."""

from __future__ import annotations

import argparse

from kalan.commands import Out, arg, command, money_for, percent, shorten, table
from kalan.service import Kalan, check_month


@command("ay", "month", order=40, help="spending of one month by category", args=[
    arg("month", nargs="?", help="YYYY-MM (default: this month)"),
])
def month(k: Kalan, a: argparse.Namespace) -> Out:
    s = k.month(check_month(a.month))
    money = money_for(k)
    cur = s["currency"]
    head = f"{s['month']}: spent {money(s['total_spend'], cur)}, credits {money(s['total_credit'], cur)}"
    prev = s["previous_month"]
    if prev is not None:
        head += f", previous month {money(prev['total_spend'], cur)} ({percent(prev['change'])})"
    lines = [head]
    rows = [[c["category"], money(c["amount"]), percent(c["share"])] for c in s["by_category"]]
    if rows:
        lines += ["", *table(["Category", "Amount", "Share"], rows, right=[1, 2])]
    return Out(s, lines)


@command("abonelikler", "subscriptions", order=50, help="recurring charges and their price changes")
def subscriptions(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.subscriptions()
    money = money_for(k)
    rows = [[shorten(i["merchant"], 32), money(i["amount_last"], i["currency"]),
             money(i["amount_previous"], i["currency"]) if i["amount_previous"] is not None else "",
             percent(i["price_change"]) if i["price_change"] is not None else "", len(i["months_seen"]),
             i["months_seen"][-1] if i["months_seen"] else ""] for i in items]
    lines = table(["Merchant", "Last", "Before", "Change", "Months", "Last seen"], rows, right=[1, 2, 3, 4])
    return Out({"items": items}, lines or ["No recurring charges found."])


@command("islemler", "transactions", order=45, help="transactions, newest first; filter by month or text", args=[
    arg("--month", help="YYYY-MM: only the transactions that count in this statement month"),
    arg("-s", "--search", help="text in the description, merchant or category (accents ignored)"),
    arg("--limit", type=int, default=50, help="at most this many rows (default 50, 0 for all)"),
])
def transactions(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.transactions(check_month(a.month), a.search, a.limit or None)
    money = money_for(k)
    rows = [[t["date"], t["month"], shorten(t["description"]), ("-" if t["direction"] == "credit" else "")
             + money(t["amount"], t["currency"]), t["kind"], t.get("category") or ""] for t in items]
    lines = table(["Date", "Month", "Description", "Amount", "Kind", "Category"], rows, right=[3])
    return Out({"items": items}, lines or ["No transactions match."])
