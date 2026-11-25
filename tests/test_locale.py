"""Any ISO 4217 currency marker and any date order in the amount/date layer."""

from __future__ import annotations

from datetime import date

import pytest

from kalan.amounts import COMMA_DECIMAL, DOT_DECIMAL, AmountFormat, parse_amount, parse_date
from kalan.store import Settings


@pytest.mark.parametrize(
    ("text", "fmt", "minor", "sign", "currency"),
    [
        ("ILS 1,234.56", DOT_DECIMAL, 123456, None, "ILS"),
        ("1.234,56 BRL", COMMA_DECIMAL, 123456, None, "BRL"),
        ("-MXN 12.00", DOT_DECIMAL, 1200, "-", "MXN"),
        ("ARS 1.500,00", COMMA_DECIMAL, 150000, None, "ARS"),
        ("₪ 89.90", DOT_DECIMAL, 8990, None, "₪"),
        ("R$ 1.234,56", COMMA_DECIMAL, 123456, None, "R$"),
        ("1.234,56 TL", COMMA_DECIMAL, 123456, None, "TL"),
        ("+2,345.60", DOT_DECIMAL, 234560, "+", None),
        ("JPY 1500", AmountFormat(thousands=",", decimal=".", decimals=0), 1500, None, "JPY"),
    ],
)
def test_any_iso_currency_marker(text, fmt, minor, sign, currency):
    p = parse_amount(text, fmt)
    assert (p.minor, p.sign, p.currency) == (minor, sign, currency)


def test_lower_case_letters_are_not_a_currency():
    with pytest.raises(ValueError):
        parse_amount("12.00 abc", DOT_DECIMAL)


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        ("02/13/2026", "MM/DD/YYYY", date(2026, 2, 13)),
        ("13/02/2026", "DD/MM/YYYY", date(2026, 2, 13)),
        ("2026-02-13", "YYYY-MM-DD", date(2026, 2, 13)),
        ("20260213", "YYYYMMDD", date(2026, 2, 13)),
        ("13 fev 2026", "D MMM YYYY", date(2026, 2, 13)),
        ("13 de febrero de 2026", "D de MMM de YYYY", date(2026, 2, 13)),
        ("13 פברואר 2026", "D MMM YYYY", date(2026, 2, 13)),
        ("13/02/26", "DD/MM/YY", date(2026, 2, 13)),
    ],
)
def test_any_date_order(text, fmt, expected):
    assert parse_date(text, fmt) == expected


def test_settings_accept_any_iso_currency_and_new_locales():
    for code in ("BRL", "MXN", "ARS", "ILS", "JPY"):
        assert Settings(base_currency=code).base_currency == code
    for loc in ("pt", "es", "he"):
        assert Settings(locale=loc).locale == loc
    with pytest.raises(ValueError):
        Settings(base_currency="real")
