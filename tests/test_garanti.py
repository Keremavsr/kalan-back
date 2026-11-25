"""Synthetic Garanti BBVA Bonus fixtures: parser, reconciliation and plans against generated ground truth.

Fixtures come from ``tools/synth_garanti.py``; the ground truth is that script's own simulation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kalan.parsers import detect_parser
from kalan.pdf import extract_text
from kalan.redact import redact_text
from tests import bank_truth as bt

FIXTURES = Path(__file__).parent / "fixtures" / "garanti"
PDFS = sorted(FIXTURES.glob("gb-*.pdf"))
N = 12


def _text(i: int) -> str:
    return redact_text(extract_text(PDFS[i]))


def test_fixture_count_and_scenarios():
    assert len(PDFS) == N
    tags = " | ".join(t for _, truth in bt.parsed(FIXTURES) for t in truth["scenarios"])
    for needle in ("zero balance", "installment start", "last installment", "refund", "annual fee", "BSMV",
                   "partial payment", "bonus spent row", "foreign currency", "table over a page break",
                   "virtual card section", "two plans same shop and day"):
        assert needle in tags


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
    assert set(bt.plan_map(FIXTURES).values()) == {"P1", "P2", "P3", "P4"}


def test_no_personal_data_survives():
    bt.check_no_personal_data(FIXTURES, ("ORNEK KISI", "5400 61", "ORNEKKOY", "12345678", "ABNO 1", "FTNO 1"), "1111")


def test_detection_is_exclusive():
    assert detect_parser(_text(3)).id == "tr_garanti_bonus"


def test_bonus_only_row_has_no_tl_amount():
    st, _ = bt.parsed(FIXTURES)[6]
    rows = [t for t in st.transactions if t.kind == "other"]
    assert len(rows) == 1 and rows[0].amount == 0 and rows[0].points.endswith("-")
