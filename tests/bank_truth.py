"""Ground-truth checks shared by the synthetic Yapı Kredi and Garanti BBVA fixture tests.

Each fixture folder holds ``*.pdf`` with a ``*.json`` ground truth written by the generator in
``tools/``. Plan ids are parser-defined hashes, so they are compared through the ground truth's
``plan_key``: every key maps to exactly one plan id across the folder, and vice versa.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from kalan.models import Statement
from kalan.pipeline import parse_pdf
from kalan.reconcile import buckets
from kalan.schedule import plan_states

TX_FIELDS = (
    "date", "description", "amount", "currency", "direction", "kind", "merchant", "source", "original_amount",
    "original_currency", "points", "detail",
)
INSTALLMENT_FIELDS = ("remaining_count", "remaining_amount_each", "is_last", "index", "total_count", "component")


@cache
def parsed(folder: Path) -> tuple[tuple[Statement, dict], ...]:
    out = []
    for pdf in sorted(folder.glob("*.pdf")):
        truth = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
        out.append((parse_pdf(pdf), truth))
    return tuple(out)


@cache
def plan_map(folder: Path) -> dict[str, str]:
    """plan_id -> plan_key, checked to be a bijection over all statements of the folder."""
    mapping: dict[str, str] = {}
    reverse: dict[str, str] = {}
    for st, truth in parsed(folder):
        for tx, exp in zip(st.transactions, truth["transactions"], strict=True):
            if exp["installment"] is None:
                continue
            pid, key = tx.installment.plan_id, exp["installment"]["plan_key"]
            assert mapping.setdefault(pid, key) == key, f"plan id {pid} used for {key} and {mapping[pid]}"
            assert reverse.setdefault(key, pid) == pid, f"plan {key} has two ids"
    return mapping


def check_statement(folder: Path, i: int) -> None:
    st, truth = parsed(folder)[i]
    got = st.model_dump(mode="json")
    for f, value in truth["statement"].items():
        assert got[f] == value, f
    assert st.id.startswith("st_")


def check_transactions(folder: Path, i: int) -> None:
    st, truth = parsed(folder)[i]
    assert len(st.transactions) == len(truth["transactions"])
    ids = plan_map(folder)
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


def check_reconciliation(folder: Path, i: int) -> None:
    st, truth = parsed(folder)[i]
    exp = truth["expected_reconciliation"]
    r = st.reconciliation
    assert (r.status, r.expected, r.computed, r.difference) == (
        exp["status"], exp["expected"], exp["computed"], exp["difference"]
    ), r.checks
    assert [c.name for c in r.checks] == exp["checks"]
    assert all(c.status == "ok" for c in r.checks)
    assert buckets(st) == exp["buckets"]


def check_open_plans(folder: Path, i: int) -> None:
    """Plans still running after statement ``i``, read from statements 1..i."""
    statements = [st for st, _ in parsed(folder)[: i + 1]]
    ids = plan_map(folder)
    got = {
        ids[pid]: {"remaining_count": s.remaining_count, "amount_each": s.amount_each}
        for pid, s in plan_states(statements).items()
        if s.remaining_count > 0
    }
    assert got == parsed(folder)[i][1]["open_plans"]


def check_no_personal_data(folder: Path, needles: tuple[str, ...], last4: str) -> None:
    for st, _ in parsed(folder):
        blob = st.model_dump_json()
        for needle in needles:
            assert needle not in blob, needle
        assert st.card_last4 == last4
