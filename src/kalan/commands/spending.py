"""``kalan ay`` (monthly summary), ``kalan abonelikler`` (subscriptions), ``kalan islemler`` (transactions)."""

from __future__ import annotations

import argparse

from kalan.commands import Out, arg, cols, command, money_for, percent, shorten, table
from kalan.i18n import t, word
from kalan.service import Kalan, check_month


@command("ay", "month", order=40, help="help.ay", args=[
    arg("month", nargs="?", help="help.ay.month"),
])
def month(k: Kalan, a: argparse.Namespace) -> Out:
    s = k.month(check_month(a.month))
    money = money_for(k)
    cur = s["currency"]
    head = t("month.head", month=s["month"], spend=money(s["total_spend"], cur), credit=money(s["total_credit"], cur))
    prev = s["previous_month"]
    if prev is not None:
        head += t("month.previous", spend=money(prev["total_spend"], cur), change=percent(prev["change"]))
    lines = [head]
    rows = [[word("category", c["category"]), money(c["amount"]), percent(c["share"])] for c in s["by_category"]]
    if rows:
        lines += ["", *table(cols("category", "amount", "share"), rows, right=[1, 2])]
    return Out(s, lines)


@command("abonelikler", "subscriptions", order=50, help="help.abonelikler")
def subscriptions(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.subscriptions()
    money = money_for(k)
    rows = [[shorten(i["merchant"], 32), money(i["amount_last"], i["currency"]),
             money(i["amount_previous"], i["currency"]) if i["amount_previous"] is not None else "",
             percent(i["price_change"]) if i["price_change"] is not None else "", len(i["months_seen"]),
             i["months_seen"][-1] if i["months_seen"] else ""] for i in items]
    lines = table(cols("merchant", "last", "before", "change", "months", "last_seen"), rows, right=[1, 2, 3, 4])
    return Out({"items": items}, lines or [t("subscriptions.none")])


@command("islemler", "transactions", order=45, help="help.islemler", args=[
    arg("--month", help="help.islemler.month"),
    arg("-s", "--search", help="help.islemler.search"),
    arg("--limit", type=int, default=50, help="help.islemler.limit"),
])
def transactions(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.transactions(check_month(a.month), a.search, a.limit or None)
    money = money_for(k)
    rows = [[tx["date"], tx["month"], shorten(tx["description"]), ("-" if tx["direction"] == "credit" else "")
             + money(tx["amount"], tx["currency"]), word("kind", tx["kind"]), word("category", tx.get("category"))]
            for tx in items]
    lines = table(cols("date", "month", "description", "amount", "kind", "category"), rows, right=[3])
    return Out({"items": items}, lines or [t("transactions.none")])
