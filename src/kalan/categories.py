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
    glued: tuple[str, ...]
    tails: frozenset[str]
    address: frozenset[str]
    stems: tuple[tuple[str, str], ...]
    brands: tuple[tuple[str, str], ...]
    kinds: dict[str, str]
    rules: tuple[Rule, ...]


@cache
def builtin() -> BuiltinRules:
    data = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
    merchant = data["merchant"]
    processors = sorted((fold(str(p)) for p in merchant["processors"]), key=len, reverse=True)
    bare = sorted((fold(str(p)) for p in merchant.get("bare_processors", [])), key=len, reverse=True)
    every = sorted(set(processors) | set(bare), key=len, reverse=True)
    starred = r"(?:" + "|".join(map(re.escape, every)) + r")\s*\*\s*"
    loose = r"(?:" + "|".join(map(re.escape, bare)) + r")(?:\s*[-.:/]\s*|\s+)" if bare else None
    kinds = {k: v for k, v in data["kinds"].items()}
    for category in kinds.values():
        if category not in CATEGORIES:
            raise ValueError(f"categories.yaml: unknown category {category!r}")
    rules = tuple(Rule.of(str(p), category) for category, phrases in data["rules"].items() for p in phrases)
    return BuiltinRules(
        processors=re.compile(r"^(?:" + starred + (f"|{loose}" if loose else "") + r")", re.IGNORECASE),
        glued=tuple(p for p in bare if p.isalpha() and len(p) >= GLUED_MIN),
        tails=frozenset(fold(str(t)) for t in merchant["tails"]),
        address=frozenset(fold(str(t)) for t in merchant.get("address", [])),
        stems=_stem_table(data.get("stems", []), rules),
        brands=tuple(sorted({(r.folded, r.category) for r in rules if " " not in r.folded and r.folded.isalpha()
                             and len(r.folded) >= STEM_MIN}, key=lambda kv: (-len(kv[0]), kv[0]))),
        kinds=kinds,
        rules=rules,
    )


GLUED_MIN = 5
"""Shortest processor name stripped when glued to the shop's short form (``MONEYPAYMIGR MIGROS``)."""
GLUED_REST_MIN = 3


def _strip_glued_processor(tokens: list[str], b: BuiltinRules) -> list[str]:
    """``MONEYPAYMIGR MIGROS HEMEN`` -> ``MIGROS HEMEN``; ``MONEYPAYMIGROS HEMEN`` -> ``MIGROS HEMEN``.

    Only the first word, only an unambiguous processor name from ``bare_processors``, and only when what is glued
    behind it is the start of the next word or a known brand. ``PAPARAZZI CAFE`` stays as it is."""
    if not tokens:
        return tokens
    first = fold(tokens[0])
    for proc in b.glued:
        if not first.startswith(proc) or len(first) - len(proc) < GLUED_REST_MIN:
            continue
        rest = first[len(proc):]
        if not rest.isalpha():
            return tokens
        if len(tokens) > 1 and fold(tokens[1]).startswith(rest):
            return tokens[1:]
        raw = tokens[0][len(proc):]
        if fold(raw) == rest and any(rest == brand for brand, _ in b.brands):
            return [raw, *tokens[1:]]
        return tokens
    return tokens


_NUMBER_SEGMENT = re.compile(r"(?<=\S)[-/]\d{3,}(?=[-/\s]|$)")
_NUMBER_TOKEN = re.compile(r"[\W\d_]*\d{3,}[\W\d_]*|.*\d{4,}.*")


def clean_merchant(key: str) -> str:
    """``PAYTR *ORNEK KAFE 000123 ISTANBUL TR`` -> ``ORNEK KAFE``. Never returns an empty name for a non-empty key."""
    b = builtin()
    text = key.strip()
    for _ in range(2):  # "PAYTR *IYZICO*SHOP": a processor may sit behind another
        text = b.processors.sub("", text, count=1)
    text = text.replace("*", " ")
    text = _NUMBER_SEGMENT.sub("", text)
    tokens = _strip_glued_processor(text.split(), b)
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


_NUMBERISH = re.compile(r"^(?:no[:.]?)?\d[\d/:.-]*$")


def _address_token(word: str, b: BuiltinRules) -> str | None:
    """``"marker"`` for a street/district marker (CD, SK, MAH ...), ``"place"`` for a city or district,
    ``"number"`` for a door number, else None."""
    w = fold(word).strip(".,:;-/()")
    if not w:
        return "number"
    if w in b.address:
        return "marker"
    if w in b.tails:
        return "place"
    if _NUMBERISH.match(w):
        return "number"
    return None


def merchant_name(key: str) -> str | None:
    """:func:`clean_merchant` without address parts; None when only an address is left (unreadable).

    ``ORNEK KAFE BAGDAT CD NO:5`` -> ``ORNEK KAFE``; ``ISTANBUL KADIKOY BAGDAT CD`` -> None. The word right before a
    street marker is the street's name. The first run of words that are not address parts is the merchant.
    """
    name = clean_merchant(key)
    if not name:
        return None
    b = builtin()
    tokens = name.split()
    kinds = [_address_token(t, b) for t in tokens]
    if not any(k == "marker" for k in kinds) and not all(k is not None for k in kinds):
        return name  # no street marker and some real word: keep the cleaned name as it is
    address = [k is not None for k in kinds]
    for i, k in enumerate(kinds):
        if k == "marker" and i > 0 and kinds[i - 1] is None:
            address[i - 1] = True  # the street's own name ("BAGDAT" in "BAGDAT CD")
    runs: list[list[str]] = []
    current: list[str] = []
    for token, is_address in zip(tokens, address, strict=True):
        if is_address:
            if current:
                runs.append(current)
            current = []
        else:
            current.append(token)
    if current:
        runs.append(current)
    for run in runs:
        text = " ".join(run).strip(" -/.,")
        if re.search(r"[^\W\d_]{2,}", text):
            return text
    return None


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
    stem = _stem_hit(text, b)
    if stem is not None:
        return stem, "builtin"
    return None


STEM_MIN = 5
_BRAND_TAIL = re.compile(r"[\W\d_]*(?:com|comtr|tr|ab|as|ltd|sti|inc|llc|bv|gmbh|online|store|shop|premium|plus|tv|"
                         r"go|app|pay|bill|ticaret|tic|magazacilik|magazalari|gida|elektronik|turizm|"
                         r"iletisim)?[\W\d_]*")


def _stem_table(words: list[str], rules: tuple[Rule, ...]) -> tuple[tuple[str, str], ...]:
    """``(stem, category)`` longest first; each stem's category is the one of the rule with the same phrase."""
    by_phrase = {r.folded: r.category for r in rules}
    out = {}
    for w in words:
        folded = fold(str(w))
        if folded not in by_phrase:
            raise ValueError(f"categories.yaml: stem {w!r} is not a rule phrase")
        out[folded] = by_phrase[folded]
    return tuple(sorted(out.items(), key=lambda kv: (-len(kv[0]), kv[0])))


def _stem_hit(text: str, b: BuiltinRules) -> str | None:
    """Fallback for generic words the printer glued or cut (``stems`` in ``categories.yaml``): a token that starts
    or ends with a stem (``MARKETCILIK``, ``ORNEKPASTANE``), or a last token of five or more letters cut from one
    (``RESTORA``)."""
    tokens = [t.strip(".,:;-/()'") for t in text.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return None
    last = tokens[-1]
    for word, category in b.brands:  # "SPOTIFYAB", "MIGROS5M", "TRENDYOLCOM": a brand with a known tail
        for t in tokens:
            if t != word and t.startswith(word) and _BRAND_TAIL.fullmatch(t[len(word):]):
                return category
    for word, category in b.stems:
        for t in tokens:
            if t != word and (t.startswith(word) or t.endswith(word)):
                return category
        if len(last) >= STEM_MIN and word.startswith(last) and last.isalpha():
            return category
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
