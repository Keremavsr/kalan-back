"""Date and amount helpers on the vendored Bank Statement Parsing Benchmark samples (other locales)."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from kalan.amounts import AmountFormat, parse_amount, try_parse_amount, try_parse_date
from kalan.pdf import extract_text

FIXTURES = Path(__file__).parent / "fixtures" / "bsb"
IDS = sorted(p.stem for p in FIXTURES.glob("bsb-*.json"))


@cache
def load(name: str) -> tuple[dict, list[str]]:
    meta = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    text = extract_text(FIXTURES / f"{name}.pdf")
    lines = [ln.strip() for ln in text.replace("\f", "\n").split("\n") if ln.strip()]
    return meta, lines


def fmt_of(meta: dict) -> AmountFormat:
    nf = meta["numberFormat"]
    return AmountFormat(thousands=nf["thousandsSeparator"], decimal=nf["decimalSeparator"])


def clean(line: str) -> str:
    # The benchmark PDFs print the euro sign with a font PyMuPDF maps to U+FFFD.
    return line.replace("�", "").strip()


def test_five_samples():
    assert IDS == ["bsb-001", "bsb-002", "bsb-003", "bsb-004", "bsb-005"]


@pytest.mark.parametrize("name", IDS)
def test_dates_parse_with_declared_format(name):
    meta, lines = load(name)
    fmt = meta["dateFormat"]
    n_tokens = len(fmt.split())
    found = []
    for ln in lines:
        head = " ".join(ln.split()[:n_tokens])
        d = try_parse_date(head, fmt, default_year=2025)
        if d is not None:
            found.append(d)
    assert len(found) >= meta["transactionCount"]
    assert all(2024 <= d.year <= 2026 for d in found)


@pytest.mark.parametrize("name", IDS)
def test_amounts_parse_with_declared_format(name):
    meta, lines = load(name)
    fmt = fmt_of(meta)
    amounts = [p for ln in lines if (p := try_parse_amount(clean(ln), fmt)) is not None and fmt.decimal in ln]
    assert len(amounts) >= meta["transactionCount"]
    if meta["numberFormat"].get("negativeSign") == "leading-minus":
        assert any(p.sign == "-" for p in amounts)


@pytest.mark.parametrize("name", IDS)
def test_other_format_is_not_silently_accepted(name):
    """Amounts with thousands separators must fail under the opposite format instead of being misread."""
    meta, lines = load(name)
    fmt = fmt_of(meta)
    other = AmountFormat(thousands=fmt.decimal, decimal="," if fmt.decimal == "." else ".")
    sep = fmt.thousands
    grouped = [
        clean(ln) for ln in lines
        if sep in clean(ln) and fmt.decimal in ln and (p := try_parse_amount(clean(ln), fmt)) and p.minor >= 100_000
    ]
    assert grouped
    for text in grouped:
        alt = try_parse_amount(text, other)
        assert alt is None or alt.minor != parse_amount(text, fmt).minor


def test_bsb003_balance_arithmetic():
    """Opening - outgoing + incoming = closing, using the comma-decimal helper."""
    _, lines = load("bsb-003")
    fmt = fmt_of(load("bsb-003")[0])

    def value_after(label: str) -> int:
        i = next(i for i, ln in enumerate(lines) if ln.startswith(label))
        return parse_amount(clean(lines[i + 1]), fmt).minor

    opening = value_after("Balance as of 01.05.2025")
    closing = value_after("Balance as of 31.05.2025")
    incoming = value_after("Total incoming")
    outgoing = value_after("Total outgoing")
    assert opening - outgoing + incoming == closing
