"""``kalan uyarilar``: alerts computed in code (limit, due date, interest, missing statements ...)."""

from __future__ import annotations

import argparse
from typing import Any

from kalan.alerts import Alert
from kalan.commands import Money, Out, cols, command, money_for, percent, table
from kalan.i18n import has, t, word
from kalan.service import Kalan

# Alert parameters that hold money (minor units in the alert's ``currency``) or a fraction.
MONEY_PARAMS = {"amount_each", "statement_total", "credit_limit", "total", "minimum_payment", "amount_previous",
                "amount_last"}
SHARE_PARAMS = {"used_share", "price_change"}


def alert_params(params: dict[str, Any], money: Money) -> dict[str, Any]:
    """Alert parameters ready for the catalogue text: money and shares formatted, the card named."""
    cur = params.get("currency")
    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in MONEY_PARAMS and isinstance(value, int):
            out[key] = money(value, cur)
        elif key in SHARE_PARAMS and value is not None:
            out[key] = percent(value)
        elif isinstance(value, list):
            out[key] = ", ".join(str(v) for v in value)
        else:
            out[key] = value
    if "bank" in params:
        out["card"] = params["bank"] + (f" *{params['card_last4']}" if params.get("card_last4") else "")
    return out


def describe(alert: Alert, money: Money) -> str:
    """One line of text for an alert (the web interface translates the message key instead)."""
    key = f"alert.{alert.kind}"
    if has(key):
        try:
            return t(key, **alert_params(alert.params, money))
        except (KeyError, IndexError, ValueError):
            pass
    return alert.kind.replace("_", " ")


@command("uyarilar", "alerts", order=60, help="help.uyarilar")
def alerts(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.alerts()
    money = money_for(k)
    rows = [[word("severity", al.severity), al.month, describe(al, money)] for al in items]
    lines = table(cols("severity", "month", "alert"), rows)
    return Out({"items": [al.model_dump(mode="json") for al in items]}, lines or [t("alerts.none")])
