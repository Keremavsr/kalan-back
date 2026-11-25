"""``kalan uyarilar``: alerts computed in code (limit, due date, interest, missing statements ...)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from kalan.alerts import Alert
from kalan.commands import Money, Out, command, money_for, percent, table
from kalan.service import Kalan

# One line of English text per alert kind; the web interface translates the message key instead.
TEXTS: dict[str, Callable[[dict[str, Any], Money], str]] = {
    "installment_ending": lambda p, m: f"last installment of {p['description']} ({m(p['amount_each'], p['currency'])})",
    "limit_high": lambda p, m: f"{percent(p['used_share'])} of the limit used "
                               f"({m(p['statement_total'], p['currency'])} of {m(p['credit_limit'], p['currency'])})",
    "interest_charged": lambda p, m: f"interest charged: {p['count']} rows, {m(p['total'], p['currency'])}",
    "fee_charged": lambda p, m: f"fees charged: {p['count']} rows, {m(p['total'], p['currency'])}",
    "tax_charged": lambda p, m: f"taxes charged: {p['count']} rows, {m(p['total'], p['currency'])}",
    "foreign_currency": lambda p, m: f"{p['count']} foreign currency rows ({', '.join(p['currencies'])}),"
                                     f" {m(p['total'], p['currency'])}",
    "due_soon": lambda p, m: f"payment due {p['due_date']} ({p['days_left']} days): "
                             f"{m(p['statement_total'], p['currency'])},"
                             f" minimum {m(p['minimum_payment'], p['currency'])}",
    "missing_statement": lambda p, m: f"no statement for {p['bank']}" + (f" *{p['card_last4']}" if p['card_last4']
                                                                         else ""),
    "subscription_price_rise": lambda p, m: f"{p['merchant']} went from {m(p['amount_previous'], p['currency'])}"
                                            f" to {m(p['amount_last'], p['currency'])} ({percent(p['price_change'])})",
}


def describe(alert: Alert, money: Money) -> str:
    text = TEXTS.get(alert.kind)
    if text is None:
        return alert.kind.replace("_", " ")
    try:
        return text(alert.params, money)
    except (KeyError, TypeError, ValueError):
        return alert.kind.replace("_", " ")


@command("uyarilar", "alerts", order=60,
         help="alerts: limit, due date, interest and fees, missing statements, price rises")
def alerts(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.alerts()
    money = money_for(k)
    rows = [[al.severity, al.month, describe(al, money)] for al in items]
    lines = table(["Severity", "Month", "Alert"], rows)
    return Out({"items": [al.model_dump(mode="json") for al in items]}, lines or ["No alerts."])
