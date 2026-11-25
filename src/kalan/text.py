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
