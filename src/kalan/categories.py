"""Merchant names and rule-based categories.

A printed description becomes a merchant name (:func:`clean_merchant`): payment-processor prefixes such as
``IYZICO*`` or ``PAYTR *`` go, so do the ``*`` separators, terminal and store numbers, and trailing country codes,
cities and districts. A category then comes from rules, in this order:

1. the user's own rules (stored in SQLite, ``POST /category-rules``);
2. the transaction kind (fees, interest, taxes, cash advances, payments);
3. the built-in phrases in ``categories.yaml`` (Turkish merchants first).

Phrases match whole words after :func:`kalan.text.fold`; when several match, the longest wins. Whatever no rule
covers stays uncategorised and is left to the AI analysis.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

import yaml

from kalan.ai.prompt import CATEGORIES
from kalan.models import Transaction
from kalan.text import fold

RULES_FILE = Path(__file__).with_name("categories.yaml")
CategorySource = Literal["user_rule", "kind", "builtin"]


@dataclass(frozen=True, slots=True)
class Rule:
    pattern: str  # as written
    category: str
    folded: str
    regex: re.Pattern[str]

    @classmethod
    def of(cls, pattern: str, category: str) -> Rule:
        folded = fold(pattern)
        if not re.search(r"[^\W_]", folded):
            raise ValueError("the pattern has no letters or digits")
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}")
        return cls(pattern, category, folded, re.compile(rf"(?<![a-z0-9]){re.escape(folded)}(?![a-z0-9])"))


@dataclass(frozen=True, slots=True)
class BuiltinRules:
    processors: re.Pattern[str]
    tails: frozenset[str]
    kinds: dict[str, str]
    rules: tuple[Rule, ...]


@cache
def builtin() -> BuiltinRules:
    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    merchant = data["merchant"]
    processors = sorted((fold(p) for p in merchant["processors"]), key=len, reverse=True)
    kinds = {k: v for k, v in data["kinds"].items()}
    for category in kinds.values():
        if category not in CATEGORIES:
            raise ValueError(f"categories.yaml: unknown category {category!r}")
    rules = tuple(Rule.of(str(p), category) for category, phrases in data["rules"].items() for p in phrases)
    return BuiltinRules(
        processors=re.compile(r"^(?:" + "|".join(map(re.escape, processors)) + r")\s*\*\s*", re.IGNORECASE),
        tails=frozenset(fold(str(t)) for t in merchant["tails"]),
        kinds=kinds,
        rules=rules,
    )


_NUMBER_SEGMENT = re.compile(r"(?<=\S)[-/]\d{3,}(?=[-/\s]|$)")
_NUMBER_TOKEN = re.compile(r"[\W\d_]*\d{3,}[\W\d_]*|.*\d{4,}.*")


def clean_merchant(key: str) -> str:
    """``PAYTR *ORNEK KAFE 000123 ISTANBUL TR`` -> ``ORNEK KAFE``. Never returns an empty name for a non-empty key."""
    b = builtin()
    text = b.processors.sub("", key.strip())
    text = text.replace("*", " ")
    text = _NUMBER_SEGMENT.sub("", text)
    tokens = text.split()
    if not tokens:
        return key.strip()
    tokens = tokens[:1] + [t for t in tokens[1:] if not _NUMBER_TOKEN.fullmatch(t)]
    while len(tokens) > 1:
        parts = [fold(p).strip(".,-") for p in tokens[-1].split("/")]
        if all(p in b.tails for p in parts if p) and any(parts):
            tokens.pop()
        else:
            break
    name = " ".join(tokens).strip(" -/.,")
    return name or key.strip()


def _best(rules: Iterable[Rule], text: str) -> Rule | None:
    hits = [r for r in rules if r.regex.search(text)]
    return max(hits, key=lambda r: len(r.folded), default=None)


def categorize(tx: Transaction, user_rules: Iterable[Rule] = ()) -> tuple[str, CategorySource] | None:
    """Category of ``tx`` from the rules, with where it came from; None when no rule covers it."""
    text = fold(tx.merchant or tx.description)
    hit = _best(user_rules, text)
    if hit is not None:
        return hit.category, "user_rule"
    b = builtin()
    if tx.kind in b.kinds:
        return b.kinds[tx.kind], "kind"
    hit = _best(b.rules, text)
    if hit is not None:
        return hit.category, "builtin"
    return None


def apply(transactions: Iterable[Transaction], user_rules: Iterable[Rule] = ()) -> list[Transaction]:
    """Fill ``category`` of every uncategorised transaction a rule covers; others stay as they are."""
    rules = list(user_rules)
    out = []
    for tx in transactions:
        if tx.category is None:
            found = categorize(tx, rules)
            if found is not None:
                tx = tx.model_copy(update={"category": found[0]})
        out.append(tx)
    return out


def matches(rule: Rule, tx: Transaction) -> bool:
    return rule.regex.search(fold(tx.merchant or tx.description)) is not None
