"""Synthetic VakıfBank fixtures: parser, schedule and reconciliation against generated ground truth.

Fixtures come from ``tools/synth_vakifbank.py``; the ground truth is that script's own simulation.
Plan ids are parser-defined hashes, so they are compared through the ground truth's ``plan_key``:
every key must map to exactly one plan id across all 15 statements, and vice versa.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest

from kalan.models import Statement
from kalan.pipeline import parse_pdf
from kalan.reconcile import buckets
from kalan.schedule import build_schedule

FIXTURES = Path(__file__).parent / "fixtures" / "vakifbank"
PDFS = sorted(FIXTURES.glob("vb-*.pdf"))
STATEMENT_FIELDS = (
    "bank", "product", "parser_id", "card_last4", "period_start", "period_end", "due_date", "currency",
    "statement_total", "minimum_payment", "credit_limit", "next_period_end", "next_due_date", "page_count",
    "interest_rates", "printed_summary",
)
TX_FIELDS = (
    "date", "description", "amount", "currency", "direction", "kind", "merchant", "source", "original_amount",
    "original_currency", "points",
)
INSTALLMENT_FIELDS = ("remaining_count", "remaining_amount_each", "is_last", "index", "total_count", "component")


@cache
def parsed() -> list[tuple[Statement, dict]]:
    out = []
    for pdf in PDFS:
        truth = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
        out.append((parse_pdf(pdf), truth))
    return out


@cache
def plan_map() -> dict[str, str]:
    """plan_id -> plan_key, checked to be a bijection over all statements."""
    mapping: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for st, truth in parsed():
        for tx, exp in zip(st.transactions, truth["transactions"], strict=True):
            if exp["installment"] is None:
                continue
            pid, key = tx.installment.plan_id, exp["installment"]["plan_key"]
            assert mapping.setdefault(pid, key) == key, f"plan id {pid} used for {key} and {mapping[pid]}"
            assert reverse.setdefault(key, pid) == pid, f"plan {key} has two ids"
    return mapping


def test_fixture_count():
    assert len(PDFS) == 15


def test_scenarios_covered():
    tags = " | ".join(t for _, truth in parsed() for t in truth["scenarios"])
    for needle in ("zero balance", "two-page table", "early payoff", "last installment", "tax payment", "refund",
                   "foreign currency", "subscription price rise", "BSMV/KKDF/vade farkı", "installment start"):
        assert needle in tags
    assert {truth["statement"]["page_count"] for _, truth in parsed()} == {1, 2}


@pytest.mark.parametrize("i", range(15))
def test_statement_fields(i):
    st, truth = parsed()[i]
    got = st.model_dump(mode="json")
    assert got["printed_summary"].pop("remaining_installments") is None  # VakıfBank does not print it
    for f in STATEMENT_FIELDS:
        assert got[f] == truth["statement"][f], f
    assert st.id.startswith("st_")


@pytest.mark.parametrize("i", range(15))
def test_transactions_every_field(i):
    st, truth = parsed()[i]
    assert len(st.transactions) == len(truth["transactions"])
    ids = plan_map()
    for n, (tx, exp) in enumerate(zip(st.transactions, truth["transactions"], strict=True)):
        got = tx.model_dump(mode="json")
        for f in TX_FIELDS:
            assert got[f] == exp[f], f"row {n} field {f}"
        assert tx.category is None
        if exp["installment"] is None:
            assert tx.installment is None, f"row {n}"
        else:
            for f in INSTALLMENT_FIELDS:
                assert got["installment"][f] == exp["installment"][f], f"row {n} installment {f}"
            assert ids[tx.installment.plan_id] == exp["installment"]["plan_key"]


def test_plan_ids_are_bijective():
    assert set(plan_map().values()) == {"A", "B", "C", "D", "E", "CA1", "CA2"}


@pytest.mark.parametrize("i", range(15))
def test_reconciliation(i):
    st, truth = parsed()[i]
    exp = truth["expected_reconciliation"]
    r = st.reconciliation
    assert (r.status, r.expected, r.computed, r.difference) == (
        exp["status"], exp["expected"], exp["computed"], exp["difference"]
    )
    assert buckets(st) == exp["buckets"]


def _normalise_schedule(schedule: dict, ids: dict[str, str]) -> dict:
    def key(pid):
        return ids.get(pid, pid)

    months = [
        {
            "month": m["month"],
            "total": m["total"],
            "items": sorted(
                ({**{k: v for k, v in it.items() if k not in ("plan_id", "card_id", "card_label")},
                  "plan_key": key(it["plan_id"])}
                 for it in m["items"]),
                key=lambda x: x["plan_key"],
            ),
        }
        for m in schedule["months"]
    ]
    plans = sorted(
        (
            {
                "plan_key": key(p["plan_id"]), "description": p["description"], "kind": p["kind"],
                "remaining_count": p["remaining_count"], "amount_each": p["amount_each"], "ends": p["ends"],
                "total_remaining": p["total_remaining"], "breakdown": p["breakdown"],
            }
            for p in schedule["plans"]
        ),
        key=lambda p: p["plan_key"],
    )
    return {"start_month": schedule["start_month"], "months": months, "plans": plans}


@pytest.mark.parametrize("i", range(15))
def test_schedule_after_each_statement(i):
    statements = [st for st, _ in parsed()[: i + 1]]
    truth = parsed()[i][1]["expected_schedule"]
    got = build_schedule(statements, months=12).model_dump(mode="json")
    expected = {
        "start_month": truth["start_month"],
        "months": [
            {"month": m["month"], "total": m["total"], "items": sorted(m["items"], key=lambda x: x["plan_key"])}
            for m in truth["months"]
        ],
        "plans": sorted(truth["plans"], key=lambda p: p["plan_key"]),
    }
    assert _normalise_schedule(got, plan_map()) == expected


def test_no_personal_data_survives():
    for st, _ in parsed():
        blob = st.model_dump_json()
        for needle in ("ORNEK KISI", "4799*", "ORNEKKOY", "1234567890", "ORNEK SUBESI"):
            assert needle not in blob
        assert st.card_last4 == "0000"
