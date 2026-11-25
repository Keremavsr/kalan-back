"""Remove personal data from statement text before anything is parsed, stored or sent.

Rules, applied in this order:

1. ``name_address``: the block that starts with ``Sayın`` (name, then address lines) up to the next
   bank line, the table header or a date line.
2. ``labelled``: values printed after personal labels such as ``Müşteri No`` or ``Şube Adı``.
3. ``card``: card numbers, masked (``4799********1234``) or full (Luhn-valid). Replaced by
   ``[CARD ****1234]`` so that parsers can still keep the last four digits, and nothing else.
4. ``iban``: ``TR`` + 24 digits, and other IBANs that pass the mod-97 check.
5. ``email``.
6. ``national_id``: Turkish T.C. kimlik numbers (11 digits with valid check digits).
7. ``phone``: Turkish landline/mobile/0850 numbers and ``+<country>`` international numbers.
8. ``long_number``: any other run of 10 or more digits (customer, contract, barcode numbers).

Amounts and dates are never touched: they always contain a separator that the number rules exclude.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from kalan.text import fold

CARD_TOKEN = "[CARD ****{last4}]"
_MASK = "*Xx•"

_NAME_STOP_WORDS = ("vakifbank", "worldcard", "islem tarihi", "kredi karti", "hesap ozeti", "bank", "banka")
_DATE_LINE = re.compile(r"^\d{2}[./]\d{2}[./]\d{4}$")
_PERSONAL_LABELS = (
    "musteri no",
    "musteri numarasi",
    "sube adi",
    "ad soyad",
    "adi soyadi",
    "adres",
    "tc kimlik",
    "t.c. kimlik",
    "kimlik no",
    "hesap no",
    "vergi kimlik",
)
_MAX_ADDRESS_LINES = 8


def _is_personal_label(folded_label: str) -> bool:
    return len(folded_label) <= 40 and any(lab in folded_label for lab in _PERSONAL_LABELS)


_CARD_GROUPED = re.compile(
    rf"(?<![\d{_MASK}])(\d{{4}})([ -])([\d{_MASK}]{{4}})\2([\d{_MASK}]{{4}})\2(\d{{4}})(?![\d{_MASK}])"
)
_CARD_AMEX = re.compile(rf"(?<![\d{_MASK}])(\d{{4}}) ([\d{_MASK}]{{6}}) ([\d{_MASK}]{{1}})(\d{{4}})(?![\d{_MASK}])")
_CARD_PLAIN = re.compile(rf"(?<![\d{_MASK}])(\d[\d{_MASK}]{{11,17}}\d)(?![\d{_MASK}])")
_IBAN_TR = re.compile(r"\bTR ?\d{2}(?: ?\d{4}){5} ?\d{2}\b", re.IGNORECASE)
_IBAN_ANY = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TCKN = re.compile(r"(?<![\d.,])[1-9]\d{10}(?![\d]|[.,]\d)")
_PHONE_TR = re.compile(
    r"(?<![\d.,/])(?:\+90[ -]?|0[ -]?)?\(?(?:5\d{2}|[2-4]\d{2}|8[05]0)\)?"
    r"[ -]?\d{3}[ -]?\d{1,2}[ -]?\d{2,3}(?![\d]|[.,]\d)"
)
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ -]?\(?\d{1,4}\)?){2,5}(?![\d]|[.,]\d)")
_LONG_NUMBER = re.compile(r"(?<![\d.,])\d{10,}(?![\d]|[.,]\d)")


@dataclass
class RedactionResult:
    text: str
    counts: Counter[str] = field(default_factory=Counter)
    card_last4: str | None = None


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def iban_ok(iban: str) -> bool:
    s = re.sub(r"\s", "", iban).upper()
    if not 15 <= len(s) <= 34:
        return False
    rearranged = s[4:] + s[:4]
    try:
        number = int("".join(str(int(ch, 36)) for ch in rearranged))
    except ValueError:
        return False
    return number % 97 == 1


def tckn_ok(n: str) -> bool:
    if len(n) != 11 or not n.isdigit() or n[0] == "0":
        return False
    d = [int(c) for c in n]
    if ((sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10) != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def _card_candidate(raw: str) -> str | None:
    """Return the last four digits if ``raw`` is a card number, else None."""
    compact = re.sub(r"[ -]", "", raw)
    if not 13 <= len(compact) <= 19 or not compact[-4:].isdigit():
        return None
    masked = any(c in _MASK for c in compact)
    if masked:
        if not compact[:4].isdigit():
            return None
        return compact[-4:]
    return compact[-4:] if luhn_ok(compact) else None


class _Redactor:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.card_last4: str | None = None

    def sub(self, rule: str, pattern: re.Pattern[str], text: str, repl, check=None) -> str:
        def _r(m: re.Match[str]) -> str:
            if check is not None and not check(m):
                return m.group(0)
            self.counts[rule] += 1
            return repl(m) if callable(repl) else repl

        return pattern.sub(_r, text)

    def card(self, m: re.Match[str]) -> str:
        last4 = _card_candidate(m.group(0))
        if last4 is None:
            return m.group(0)
        self.counts["card"] += 1
        if self.card_last4 is None:
            self.card_last4 = last4
        return CARD_TOKEN.format(last4=last4)

    def lines(self, text: str) -> str:
        lines = text.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            f = fold(line)
            if f.startswith("sayin ") or f == "sayin":
                out.append("Sayın [NAME]")
                self.counts["name_address"] += 1
                i += 1
                taken = 0
                while i < len(lines) and taken < _MAX_ADDRESS_LINES:
                    nxt = fold(lines[i])
                    if not nxt:
                        i += 1
                        continue
                    if any(w in nxt for w in _NAME_STOP_WORDS) or _DATE_LINE.match(nxt):
                        break
                    taken += 1
                    i += 1
                if taken:
                    out.append("[ADDRESS]")
                continue
            # "Label" / ":" / "value" on separate lines, or "Label: value" inline.
            if i + 2 < len(lines) and lines[i + 1].strip() == ":" and _is_personal_label(f):
                out.extend([line, lines[i + 1], "[REDACTED]"])
                self.counts["labelled"] += 1
                i += 3
                continue
            head, sep, tail = line.partition(":")
            if sep and tail.strip() and _is_personal_label(fold(head)):
                out.append(head + ": [REDACTED]")
                self.counts["labelled"] += 1
                i += 1
                continue
            out.append(line)
            i += 1
        return "\n".join(out)


def redact(text: str) -> RedactionResult:
    """Return redacted text, how many items each rule removed, and the card's last four digits."""
    r = _Redactor()
    # Rules work line by line so a pattern never joins two lines; page breaks (\f) are kept.
    pages = []
    for page in text.split("\f"):
        page = r.lines(page)
        page = _CARD_GROUPED.sub(r.card, page)
        page = _CARD_AMEX.sub(r.card, page)
        page = _CARD_PLAIN.sub(r.card, page)
        page = r.sub("iban", _IBAN_TR, page, "[IBAN]")
        page = r.sub("iban", _IBAN_ANY, page, "[IBAN]", check=lambda m: iban_ok(m.group(0)))
        page = r.sub("email", _EMAIL, page, "[EMAIL]")
        page = r.sub("national_id", _TCKN, page, "[TCKN]", check=lambda m: tckn_ok(m.group(0)))
        page = r.sub(
            "phone", _PHONE_TR, page, "[PHONE]", check=lambda m: 10 <= len(re.sub(r"\D", "", m.group(0))) <= 12
        )
        page = r.sub(
            "phone", _PHONE_INTL, page, "[PHONE]", check=lambda m: 8 <= len(re.sub(r"\D", "", m.group(0))) <= 15
        )
        page = r.sub("long_number", _LONG_NUMBER, page, "[NUMBER]")
        pages.append(page)
    return RedactionResult(text="\f".join(pages), counts=r.counts, card_last4=r.card_last4)


def redact_text(text: str) -> str:
    return redact(text).text
