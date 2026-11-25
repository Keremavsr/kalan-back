"""One test group per redaction rule. All personal data below is invented."""

import pytest

from kalan.redact import iban_ok, luhn_ok, redact, redact_text, tckn_ok

# Invented values that pass the relevant check digits.
FULL_CARD = "4111111111111111"  # standard Luhn test number
TCKN = "10000000146"  # valid checksum, not a real person
IBAN_TR = "TR330006100519786457841326"  # textbook example IBAN
IBAN_DE = "DE89370400440532013000"


def test_masked_card_keeps_only_last4():
    r = redact("Kart No\n:\n4799********1234\n")
    assert "4799" not in r.text
    assert "[CARD ****1234]" in r.text
    assert r.card_last4 == "1234"
    assert r.counts["card"] == 1


@pytest.mark.parametrize("card", ["4799 **** **** 5678", "4799-XXXX-XXXX-5678", "479912******5678"])
def test_masked_card_variants(card):
    r = redact(f"Kart: {card}")
    assert r.card_last4 == "5678"
    assert "4799" not in r.text


def test_full_card_luhn():
    assert luhn_ok(FULL_CARD)
    assert redact_text(f"card {FULL_CARD} used") == "card [CARD ****1111] used"
    assert redact_text("card 4111 1111 1111 1111 used") == "card [CARD ****1111] used"


def test_non_luhn_16_digits_is_not_a_card_but_still_removed():
    r = redact("ref 4111111111111112")
    assert r.counts["card"] == 0
    assert "[NUMBER]" in r.text


def test_name_and_address_block():
    text = (
        "Son Ödeme Tarihi: 04.05.2026\nSayın ORNEK KISI\nORNEK MAH. 1. SOKAK NO 2\n34000 ISTANBUL\n"
        "VakıfBank Worldcard'ınıza\n"
    )
    r = redact(text)
    assert "ORNEK KISI" not in r.text and "SOKAK" not in r.text and "34000" not in r.text
    assert "Sayın [NAME]\n[ADDRESS]\nVakıfBank Worldcard'ınıza" in r.text
    assert "04.05.2026" in r.text


def test_address_stops_at_table_header():
    r = redact("Sayın ORNEK KISI\nADRES SATIRI\nİŞLEM TARİHİ\n")
    assert r.text == "Sayın [NAME]\n[ADDRESS]\nİŞLEM TARİHİ\n"


def test_labelled_customer_number_and_branch():
    r = redact("VB Müşteri No\n:\n123456\nŞube Adı\n:\nORNEK SUBE\nMüşteri No: 98765\n")
    assert "123456" not in r.text and "ORNEK SUBE" not in r.text and "98765" not in r.text
    assert r.counts["labelled"] == 3


def test_vb_prefixed_label():
    # "VB Müşteri No" does not start with the label; the 10+ digit rule still removes long numbers.
    r = redact("VB Müşteri No 1234567890")
    assert "1234567890" not in r.text


def test_customer_number_10_plus_digits():
    r = redact("Barkod 0012345678901234 ve 9876543210")
    assert r.text == "Barkod [NUMBER] ve [NUMBER]"
    assert r.counts["long_number"] == 2


def test_iban_tr_and_generic():
    assert iban_ok(IBAN_TR) and iban_ok(IBAN_DE)
    r = redact(f"IBAN {IBAN_TR} / TR33 0006 1005 1978 6457 8413 26 / {IBAN_DE}")
    assert r.text == "IBAN [IBAN] / [IBAN] / [IBAN]"
    assert r.counts["iban"] == 3


def test_generic_iban_needs_checksum():
    assert not iban_ok("DE00370400440532013000")
    assert "DE00" in redact_text("code DE00ABCD")


def test_national_id():
    assert tckn_ok(TCKN)
    assert not tckn_ok("12345678901")
    r = redact(f"TC {TCKN}")
    assert r.text == "TC [TCKN]"
    assert r.counts["national_id"] == 1


@pytest.mark.parametrize(
    "phone",
    ["0532 123 45 67", "+90 532 123 45 67", "05321234567", "0850 222 0 724", "(0212) 555 12 34", "+44 20 7946 0958"],
)
def test_phone_numbers(phone):
    r = redact(f"Tel: {phone} ara")
    assert "[PHONE]" in r.text
    assert not any(ch.isdigit() for ch in r.text.replace("[PHONE]", ""))


def test_email():
    r = redact("mail ornek.kisi+test@example.com.tr bitti")
    assert r.text == "mail [EMAIL] bitti"


@pytest.mark.parametrize(
    "line",
    [
        "17.03.2026",
        "2,345.60",
        "+12,804.33",
        "2x1,250.40",
        "%3.11    %37.32",
        "Sayfa 1 / 2",
        "61,500.00 TL",
        "1,234,567.89",
        "Vergi Th/[NUMBER]/0042",
        "MARKET-ORNEK 2/6",
    ],
)
def test_amounts_dates_and_cells_untouched(line):
    assert redact_text(line) == line


def test_page_breaks_kept():
    assert redact_text("a\fb") == "a\fb"
