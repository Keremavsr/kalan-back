from datetime import date

import pytest

from kalan.amounts import (
    COMMA_DECIMAL,
    DOT_DECIMAL,
    SPACE_COMMA,
    AmountFormat,
    add_months,
    format_minor,
    months_between,
    parse_amount,
    parse_date,
    try_parse_amount,
)
from kalan.text import fold


@pytest.mark.parametrize(
    ("text", "minor", "sign"),
    [
        ("2,345.60", 234560, None),
        ("+12,804.33", 1280433, "+"),
        ("0.00", 0, None),
        ("61.08", 6108, None),
        ("1,234,567.89", 123456789, None),
        ("2,345.60 TL", 234560, None),
        ("-12.50", 1250, "-"),
        ("12.50-", 1250, "-"),
        ("12.50+", 1250, "+"),
        ("999", 99900, None),
    ],
)
def test_dot_decimal(text, minor, sign):
    p = parse_amount(text, DOT_DECIMAL)
    assert (p.minor, p.sign) == (minor, sign)


@pytest.mark.parametrize(
    ("text", "minor"),
    [("2.345,60", 234560), ("R$ 2.345,60", 234560), ("15.226,70", 1522670), ("0,99", 99), ("-6,90 €", 690)],
)
def test_comma_decimal(text, minor):
    assert parse_amount(text, COMMA_DECIMAL).minor == minor


def test_space_thousands():
    assert parse_amount("1 234,56 $", SPACE_COMMA).minor == 123456
    assert parse_amount("- 6 886,21 $", SPACE_COMMA).signed == -688621
    assert parse_amount("1 234,56", SPACE_COMMA).minor == 123456


@pytest.mark.parametrize("text", ["2.345,60", "2,34,5.60", "12.3", "abc", "", "2x1,250.40", "17.03.2026"])
def test_dot_decimal_rejects_other_formats(text):
    assert try_parse_amount(text, DOT_DECIMAL) is None


def test_comma_decimal_rejects_dot_decimal():
    assert try_parse_amount("2,345.60", COMMA_DECIMAL) is None


def test_explicit_format_no_guessing():
    # "2.345" is 2,345 units in COMMA_DECIMAL but invalid (3 decimals) in DOT_DECIMAL.
    assert parse_amount("2.345", COMMA_DECIMAL).minor == 234500
    assert try_parse_amount("2.345", DOT_DECIMAL) is None


def test_signed_value():
    assert parse_amount("-1.00", DOT_DECIMAL).signed == -100
    assert parse_amount("+1.00", DOT_DECIMAL).signed == 100


def test_same_separators_rejected():
    with pytest.raises(ValueError):
        AmountFormat(thousands=".", decimal=".")


def test_format_minor_roundtrip():
    for v in (0, 5, 99, 100, 234560, 1280433, 123456789):
        assert parse_amount(format_minor(v), DOT_DECIMAL).minor == v
        assert parse_amount(format_minor(v, COMMA_DECIMAL), COMMA_DECIMAL).minor == v


@pytest.mark.parametrize(
    ("text", "fmt", "year", "expected"),
    [
        ("17.03.2026", "DD.MM.YYYY", None, date(2026, 3, 17)),
        ("31/01/2025", "DD/MM/YYYY", None, date(2025, 1, 31)),
        ("01/15", "MM/DD", 2025, date(2025, 1, 15)),
        ("7 mei", "D MMM", 2025, date(2025, 5, 7)),
        ("3 Feb", "D MMM", 2024, date(2024, 2, 3)),
        ("05 JAN 25", "DD MMM YY", None, date(2025, 1, 5)),
        ("12 Aralık 2025", "D MMM YYYY", None, date(2025, 12, 12)),
        ("1 Şubat 2026", "D MMM YYYY", None, date(2026, 2, 1)),
    ],
)
def test_parse_date(text, fmt, year, expected):
    assert parse_date(text, fmt, default_year=year) == expected


def test_parse_date_needs_year():
    with pytest.raises(ValueError):
        parse_date("7 mei", "D MMM")


def test_month_math():
    assert add_months("2026-11", 2) == "2027-01"
    assert add_months("2026-01", -1) == "2025-12"
    assert months_between("2025-12", "2026-02") == 2


def test_fold_mixed_i():
    assert fold("Dönem Içi Islemler") == fold("DÖNEM İÇİ İŞLEMLER") == "donem ici islemler"
    assert fold("Toplam Faiz ve Ücretler") == "toplam faiz ve ucretler"
