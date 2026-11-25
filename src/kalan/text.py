"""Text normalisation used to match printed labels robustly.

Bank PDFs mix dotted and undotted I (``Dönem Içi Islemler`` next to ``İŞLEM TARİHİ``) and sometimes
drop diacritics. ``fold`` maps every variant to plain lower-case text so labels can be compared
without caring how a particular statement spelled them.
"""

from __future__ import annotations

import re
import unicodedata

_I_MAP = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
_SPACE_RE = re.compile(r"\s+")


def fold(text: str) -> str:
    """Casefold, strip diacritics, unify Turkish I variants and collapse whitespace."""
    text = text.translate(_I_MAP)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    return _SPACE_RE.sub(" ", text).strip()


def same_label(a: str, b: str) -> bool:
    return fold(a) == fold(b)


_INSTALLMENT_MARKERS = re.compile(
    r"\(?\b\d{1,2}\s*/\s*\d{1,2}\b\)?|\b\d{1,2}\s*\.\s*taks[iİı]t\b|\btaks[iİı]t\b",
    re.IGNORECASE,
)
_FOREIGN_TAIL = re.compile(r"\s+\d[\d,.]*\s+[A-Z]{3}$")


def merchant_key(description: str) -> str:
    """Description without installment markers or foreign-amount tail, upper case.

    The stem of installment plan ids (AI fallback, imports): keep it stable, stored plan ids depend on it.
    """
    text = _FOREIGN_TAIL.sub("", description.strip())
    text = _INSTALLMENT_MARKERS.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip(" -/")
    return text.upper()


def normalize_merchant(description: str) -> str:
    """:func:`merchant_key` without processor prefixes, ``*``, store numbers, city, country and address parts.

    ``IYZICO*ORNEK KAFE 000123 ISTANBUL TR`` -> ``ORNEK KAFE``; see :mod:`kalan.categories`. Empty when the
    description holds only an address (no readable merchant name).
    """
    from kalan.categories import merchant_name  # late: kalan.categories imports this module

    return merchant_name(merchant_key(description)) or ""


def merchant_fields(description: str) -> dict[str, str | bool | None]:
    """``{"merchant", "merchant_unreadable"}`` for a row that should carry a merchant name."""
    if not merchant_key(description):
        return {"merchant": None, "merchant_unreadable": False}
    name = normalize_merchant(description)
    return {"merchant": name or None, "merchant_unreadable": not name}
