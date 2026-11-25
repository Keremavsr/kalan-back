"""Turkish credit card rules (interest caps, taxes on interest, minimum payment ratios) from a dated data file.

Order of precedence for every value: a user override stored in settings, then what the statement
prints (rate, minimum payment), then the legal maximum in ``data/tr_card_rules.json``. Each lookup
returns the value together with where it came from, so responses can say it.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

OVERRIDES_KEY = "insights.rule_overrides"
_RATE = r"^\d{1,2}(?:\.\d{1,4})?$"
_SHARE = r"^0(?:\.\d{1,4})?$|^1(?:\.0{1,4})?$"


class RuleOverrides(BaseModel):
    """User overrides (settings). Rates are percent per month as strings, shares are fractions (``"0.15"``)."""

    model_config = ConfigDict(extra="forbid")
    contractual_monthly: str | None = Field(default=None, pattern=_RATE)
    late_monthly: str | None = Field(default=None, pattern=_RATE)
    bsmv: str | None = Field(default=None, pattern=_SHARE)
    kkdf: str | None = Field(default=None, pattern=_SHARE)
    minimum_ratio: str | None = Field(default=None, pattern=_SHARE)


class Sourced(BaseModel):
    value: str
    source: str = Field(description="override | statement | rules | assumption")
    detail: str | None = None


@cache
def load_json(name: str) -> dict[str, Any]:
    raw = resources.files("kalan.insights.data").joinpath(name).read_text(encoding="utf-8")
    return json.loads(raw)


def card_rules() -> dict[str, Any]:
    return load_json("tr_card_rules.json")


def _set_for(sets: list[dict[str, Any]], on: date) -> dict[str, Any]:
    ordered = sorted(sets, key=lambda s: s["effective_from"])
    chosen = ordered[0]
    for s in ordered:
        if date.fromisoformat(s["effective_from"]) <= on:
            chosen = s
    return chosen


def max_contractual_rate(debt: int, on: date, cash_advance: bool = False) -> Sourced:
    """Legal maximum monthly contractual rate (percent) for a debt of ``debt`` kuruş on ``on``."""
    s = _set_for(card_rules()["interest"]["sets"], on)
    if cash_advance:
        return Sourced(value=s["cash_advance_monthly"], source="rules", detail=f"TCMB cap from {s['effective_from']}")
    for tier in s["tiers"]:
        if tier["below"] is None or debt < tier["below"]:
            return Sourced(value=tier["contractual_monthly"], source="rules",
                           detail=f"TCMB cap from {s['effective_from']}, debt tier below {tier['below']}")
    raise AssertionError("the last tier has no upper bound")


def contractual_rate(debt: int, on: date, printed: str | None, overrides: RuleOverrides) -> Sourced:
    """Rate actually used: override, else the printed rate capped at the legal maximum, else the maximum."""
    if overrides.contractual_monthly:
        return Sourced(value=overrides.contractual_monthly, source="override")
    cap = max_contractual_rate(debt, on)
    if printed:
        try:
            p = Decimal(printed)
        except ArithmeticError:
            return cap
        if p <= Decimal(cap.value):
            return Sourced(value=printed, source="statement")
        return Sourced(value=cap.value, source="rules", detail=f"printed {printed} is above the cap; {cap.detail}")
    return cap


def tax_shares(overrides: RuleOverrides) -> tuple[Sourced, Sourced]:
    taxes = card_rules()["taxes_on_interest"]
    bsmv = Sourced(value=overrides.bsmv, source="override") if overrides.bsmv else Sourced(
        value=taxes["bsmv"], source="rules")
    kkdf = Sourced(value=overrides.kkdf, source="override") if overrides.kkdf else Sourced(
        value=taxes["kkdf"], source="rules")
    return bsmv, kkdf


def minimum_ratio_for_limit(limit: int | None, on: date) -> Sourced | None:
    """Ratio of the statement total that must be paid at least, by card limit. ``None`` if the limit is unknown."""
    if limit is None:
        return None
    s = _set_for(card_rules()["minimum_payment"]["sets"], on)
    for tier in s["tiers"]:
        if tier["limit_up_to"] is None or limit <= tier["limit_up_to"]:
            return Sourced(value=tier["ratio"], source="rules",
                           detail=f"BDDK ratios from {s['effective_from']}, limit up to {tier['limit_up_to']}")
    raise AssertionError("the last tier has no upper bound")


def minimum_ratio(limit: int | None, total: int, printed_minimum: int, on: date,
                  overrides: RuleOverrides) -> Sourced:
    """Override, else the tier by limit, else the known ratio closest to the printed minimum, else 0.20."""
    if overrides.minimum_ratio:
        return Sourced(value=overrides.minimum_ratio, source="override")
    by_limit = minimum_ratio_for_limit(limit, on)
    if by_limit is not None:
        return by_limit
    s = _set_for(card_rules()["minimum_payment"]["sets"], on)
    known = sorted({Decimal(t["ratio"]) for t in s["tiers"]})
    if total > 0 and printed_minimum > 0:
        seen = Decimal(printed_minimum) / Decimal(total)
        best = min(known, key=lambda r: abs(r - seen))
        return Sourced(value=str(best), source="statement",
                       detail=f"no limit printed; the printed minimum is {seen:.4f} of the total")
    return Sourced(value=str(known[0]), source="assumption", detail="no limit and no minimum printed")


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def round_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_CEILING))


__all__ = [
    "OVERRIDES_KEY", "RuleOverrides", "Sourced", "card_rules", "contractual_rate", "load_json",
    "max_contractual_rate", "minimum_ratio", "minimum_ratio_for_limit", "round_half_up", "round_up", "tax_shares",
]
