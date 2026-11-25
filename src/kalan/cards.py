"""Card identity: one bank + last four digits seen on stored statements.

The id is a hash of the bank and the digits, so it stays the same across uploads and deletes. The label the
front end shows is the user's name for the card, else the short bank name and the product
(``Garanti Bonus ··1234``).
"""

from __future__ import annotations

import hashlib
from typing import Any

from kalan.models import Statement
from kalan.text import fold

NAMES_KEY = "card_names"

BANK_NAMES: dict[str, tuple[str, str]] = {
    # bank id: (full name, short name used in labels)
    "garanti": ("Garanti BBVA", "Garanti"),
    "vakifbank": ("VakıfBank", "VakıfBank"),
    "yapikredi": ("Yapı Kredi", "Yapı Kredi"),
    "akbank": ("Akbank", "Akbank"),
    "isbank": ("Türkiye İş Bankası", "İş Bankası"),
    "ziraat": ("Ziraat Bankası", "Ziraat"),
    "halkbank": ("Halkbank", "Halkbank"),
    "qnb": ("QNB", "QNB"),
    "denizbank": ("DenizBank", "DenizBank"),
    "teb": ("TEB", "TEB"),
    "ing": ("ING", "ING"),
    "hsbc": ("HSBC", "HSBC"),
    "enpara": ("Enpara", "Enpara"),
    "kuveytturk": ("Kuveyt Türk", "Kuveyt Türk"),
    "albaraka": ("Albaraka", "Albaraka"),
    "sekerbank": ("Şekerbank", "Şekerbank"),
    "odeabank": ("Odeabank", "Odeabank"),
    "fibabanka": ("Fibabanka", "Fibabanka"),
    "cepteteb": ("CEPTETEB", "CEPTETEB"),
}


def card_id(bank: str, last4: str | None) -> str:
    digest = hashlib.sha1(f"{bank}|{last4 or ''}".encode()).hexdigest()[:10]
    return f"cd_{digest}"


def statement_card_id(st: Statement) -> str:
    return card_id(st.bank, st.card_last4)


def bank_name(bank: str, short: bool = False) -> str:
    names = BANK_NAMES.get(bank)
    if names is None:
        return bank.replace("_", " ").replace("-", " ").title()
    return names[1] if short else names[0]


def label(bank: str, product: str | None, last4: str | None, name: str | None = None) -> str:
    digits = f" ··{last4}" if last4 else ""
    if name:
        return f"{name}{digits}"
    short = bank_name(bank, short=True)
    product = (product or "").strip()
    if product and not fold(product).startswith(fold(short)) and fold(product) != fold(bank):
        base = f"{short} {product}"
    else:
        base = product if product and fold(product).startswith(fold(short)) else short
    return f"{base}{digits}"


def list_cards(statements: list[Statement], names: dict[str, str]) -> list[dict[str, Any]]:
    """One entry per card of ``statements``; the product and latest statement come from the newest one."""
    groups: dict[str, list[Statement]] = {}
    for st in statements:
        groups.setdefault(statement_card_id(st), []).append(st)
    out = []
    for cid, sts in groups.items():
        sts.sort(key=lambda s: (s.period_end, s.id))
        latest = sts[-1]
        name = names.get(cid)
        out.append({
            "id": cid,
            "bank": latest.bank,
            "bank_name": bank_name(latest.bank),
            "product": latest.product,
            "card_last4": latest.card_last4,
            "name": name,
            "label": label(latest.bank, latest.product, latest.card_last4, name),
            "statement_count": len(sts),
            "first_month": sts[0].cut_off_month,
            "last_month": latest.cut_off_month,
            "latest_statement_id": latest.id,
        })
    return sorted(out, key=lambda c: (fold(c["label"]), c["id"]))


__all__ = ["BANK_NAMES", "NAMES_KEY", "bank_name", "card_id", "label", "list_cards", "statement_card_id"]
