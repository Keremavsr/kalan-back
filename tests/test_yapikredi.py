"""Synthetic Yapı Kredi World fixtures: parser, reconciliation and plans against generated ground truth.

Fixtures come from ``tools/synth_yapikredi.py``; the ground truth is that script's own simulation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kalan.parsers import detect_parser
from kalan.parsers.base import ParseError
from kalan.parsers.tr_yapikredi_world import YapikrediWorld
from kalan.pdf import extract_text
from kalan.redact import redact_text
from tests import bank_truth as bt

FIXTURES = Path(__file__).parent / "fixtures" / "yapikredi"
PDFS = sorted(FIXTURES.glob("yk-*.pdf"))
N = 12


def _text(i: int) -> str:
    return redact_text(extract_text(PDFS[i]))


def test_fixture_count_and_scenarios():
    assert len(PDFS) == N
    tags = " | ".join(t for _, truth in bt.parsed(FIXTURES) for t in truth["scenarios"])
    for needle in ("zero balance", "installment start", "last installment", "refund", "annual fee", "interest",
                   "partial payment", "digital card table", "foreign currency", "table over a page break",
                   "three-page table", "two plans same shop and day"):
        assert needle in tags
    assert max(truth["statement"]["page_count"] for _, truth in bt.parsed(FIXTURES)) >= 4


@pytest.mark.parametrize("i", range(N))
def test_statement_fields(i):
    bt.check_statement(FIXTURES, i)


@pytest.mark.parametrize("i", range(N))
def test_transactions_every_field(i):
    bt.check_transactions(FIXTURES, i)


@pytest.mark.parametrize("i", range(N))
def test_reconciliation_with_printed_summary(i):
    bt.check_reconciliation(FIXTURES, i)


@pytest.mark.parametrize("i", range(N))
def test_open_plans_after_each_statement(i):
    bt.check_open_plans(FIXTURES, i)


def test_plan_ids_are_bijective():
    assert set(bt.plan_map(FIXTURES).values()) == {"A", "B", "C", "D"}


def test_no_personal_data_survives():
    bt.check_no_personal_data(FIXTURES, ("ORNEK KISI", "5400 61", "ORNEKKOY", "12345678", "ABNO 1", "FTNO 1"), "4321")


def test_detection_is_exclusive():
    assert detect_parser(_text(3)).id == "tr_yapikredi_world"


def test_sub_lines_never_become_rows():
    st, _ = bt.parsed(FIXTURES)[1]
    bills = [t for t in st.transactions if t.detail == "ABNO [NUMBER] FTNO [NUMBER]"]
    assert bills and all(t.kind == "purchase" for t in bills)
    assert not any(t.description.startswith(("ABNO", "İşlem Tutarı")) for t in st.transactions)


def test_broken_layouts_raise():
    text = _text(3)
    with pytest.raises(ParseError):
        YapikrediWorld().parse(text.replace("TOPLAM", "").replace("PUAN ÖZETİ", "").replace("Dijital Kart", "X"))
    with pytest.raises(ParseError):
        YapikrediWorld().parse(text.replace("Hesap Kesim Tarihi", "Tarih"))
    lines = text.split("\n")
    plan_line = next(n for n, ln in enumerate(lines) if "TL'lik işlemin" in ln)
    with pytest.raises(ParseError):  # installment cell without its installment line
        YapikrediWorld().parse("\n".join(lines[:plan_line] + lines[plan_line + 1 :]))
