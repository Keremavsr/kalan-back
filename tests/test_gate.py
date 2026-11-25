"""Save gate: machine-readable codes, hard findings block the save, soft ones mark it for review."""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache
from itertools import pairwise
from pathlib import Path

import pytest

from kalan import gate
from kalan.models import InstallmentInfo, Statement, Transaction
from kalan.pipeline import parse_pdf
from kalan.reconcile import reconcile
from tests.conftest import VB_PDFS, upload

FIXTURES = Path(__file__).parent / "fixtures"


@cache
def statements(bank: str) -> tuple[Statement, ...]:
    return tuple(parse_pdf(p) for p in sorted((FIXTURES / bank).glob("*.pdf")))


def codes(issues) -> dict[str, str]:
    return {i.code: i.severity for i in issues}


@pytest.mark.parametrize("bank", ["vakifbank", "yapikredi", "garanti"])
def test_fixture_series_is_clean(bank):
    stored: list[Statement] = []
    for st in statements(bank):
        assert gate.check(st, stored) == [], st.period_end
        stored.append(st)


def base() -> Statement:
    return statements("vakifbank")[1]


def test_duplicate():
    st = base()
    other = st.model_copy(update={"id": "st_other"})
    assert codes(gate.check(st, [other])) == {"duplicate_statement": "hard"}
    assert gate.check(st, [other])[0].details == {"statement_id": "st_other"}


def test_dates():
    st = base()
    swapped = st.model_copy(update={"period_start": st.period_end, "period_end": st.period_start})
    assert codes(gate.check(swapped))["period_invalid"] == "hard"
    early_due = st.model_copy(update={"due_date": st.period_end - timedelta(days=1)})
    assert codes(gate.check(early_due))["due_before_cut_off"] == "hard"
    long = st.model_copy(update={"period_start": st.period_end - timedelta(days=90)})
    assert codes(gate.check(long))["period_unusual"] == "soft"


def test_no_rows():
    st = base().model_copy(update={"transactions": []})
    st = st.model_copy(update={"reconciliation": reconcile(st)})
    assert codes(gate.check(st))["no_transactions"] == "hard"
    zero = st.model_copy(update={"statement_total": 0, "printed_summary": None})
    zero = zero.model_copy(update={"reconciliation": reconcile(zero)})
    assert "no_transactions" not in codes(gate.check(zero))


def test_amount_scale_and_mismatch():
    st = base()
    scaled = st.model_copy(update={"statement_total": st.statement_total * 100})
    scaled = scaled.model_copy(update={"reconciliation": reconcile(scaled)})
    found = {i.code: i for i in gate.check(scaled)}
    assert found["amount_scale"].severity == "hard"
    assert found["amount_scale"].details["factor"] == 100
    off = st.model_copy(update={"statement_total": st.statement_total + 1})
    off = off.model_copy(update={"reconciliation": reconcile(off)})
    found = {i.code: i for i in gate.check(off)}
    assert set(found) == {"reconciliation_mismatch"}
    assert found["reconciliation_mismatch"].severity == "soft"
    assert "statement_total" in found["reconciliation_mismatch"].details["checks"]
    assert codes(gate.check(st.model_copy(update={"reconciliation": None}))) == {"not_reconciled": "soft"}


def test_soft_checks():
    st = base()
    assert "no_printed_summary" in codes(gate.check(st.model_copy(update={"printed_summary": None})))
    late = st.transactions[0].model_copy(update={"date": st.period_end + timedelta(days=10)})
    found = {i.code: i for i in gate.check(st.model_copy(update={"transactions": [late, *st.transactions[1:]]}))}
    assert found["rows_after_cut_off"].details == {"count": 1}
    assert "minimum_above_total" in codes(gate.check(st.model_copy(
        update={"minimum_payment": st.statement_total + 1})))


def test_installment_counter():
    st = next(s for s in statements("vakifbank") if any(t.installment and t.installment.index for t in s.transactions))
    i, tx = next((i, t) for i, t in enumerate(st.transactions) if t.installment and t.installment.index)
    broken = tx.model_copy(update={"installment": tx.installment.model_copy(
        update={"remaining_count": tx.installment.remaining_count + 1})})
    txs = [*st.transactions[:i], broken, *st.transactions[i + 1:]]
    found = {f.code: f for f in gate.check(st.model_copy(update={"transactions": txs}))}
    assert found["installment_counter"].details == {"transaction_ids": [tx.id]}


def test_installment_sequence_and_missing():
    series = statements("yapikredi")
    prev, st = next((a, b) for a, b in pairwise(series)
                    if any(t.installment and t.installment.index and not t.installment.is_last
                           for t in a.transactions))
    assert gate.check(st, [prev]) == []
    i, tx = next((i, t) for i, t in enumerate(st.transactions) if t.installment and t.installment.index)
    plan = tx.installment.plan_id
    shifted = tx.model_copy(update={"installment": tx.installment.model_copy(
        update={"index": tx.installment.index + 1, "remaining_count": max(0, tx.installment.remaining_count - 1)})})
    found = {f.code: f for f in gate.check(st.model_copy(
        update={"transactions": [*st.transactions[:i], shifted, *st.transactions[i + 1:]]}), [prev])}
    assert plan in found["installment_sequence"].details["plan_ids"]
    without = [t for t in st.transactions if not (t.installment and t.installment.plan_id == plan)]
    found = {f.code: f for f in gate.check(st.model_copy(update={"transactions": without}), [prev])}
    assert plan in found["installment_missing"].details["plan_ids"]


def test_api_blocks_hard(client, monkeypatch):
    real = gate.check

    def scaled(st, stored=()):
        bad = st.model_copy(update={"statement_total": st.statement_total * 1000})
        return real(bad.model_copy(update={"reconciliation": reconcile(bad)}), stored)

    monkeypatch.setattr(gate, "check", scaled)
    r = upload(client, VB_PDFS[2])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "save_blocked"
    assert {i["code"] for i in err["details"]["issues"]} >= {"amount_scale", "reconciliation_mismatch"}
    assert client.get("/statements").json()["items"] == []


def test_api_saves_soft_for_review(client, monkeypatch):
    real = gate.check
    monkeypatch.setattr(gate, "check", lambda st, stored=(): real(
        st.model_copy(update={"printed_summary": None}), stored))
    r = upload(client, VB_PDFS[2])
    assert r.status_code == 201
    body = r.json()
    assert body["needs_review"] is True
    assert [i["code"] for i in body["review"]] == ["no_printed_summary"]
    assert body["review"][0]["severity"] == "soft"
    listed = client.get("/statements").json()["items"][0]
    assert listed["needs_review"] is True and listed["review"] == body["review"]


def test_clean_upload_has_empty_review(client):
    body = upload(client, VB_PDFS[2]).json()
    assert body["needs_review"] is False
    assert body["review"] == []



# -- statements uploaded out of order (invented plans and amounts) ---------------------------------------


def cycle(n: int, plans: dict[str, int], total: int = 3) -> Statement:
    """Statement ``n`` (1 = 2099-01) of one invented card, printing installment ``index`` of each plan."""
    start = date(2099, n, 1)
    txs = [Transaction(id=f"tx_{n}_{plan}", date=start + timedelta(days=2), description="ORNEK MARKET",
                       amount=123456, direction="debit", kind="installment",
                       installment=InstallmentInfo(remaining_count=total - index, remaining_amount_each=123456,
                                                   is_last=index == total, plan_id=plan, index=index,
                                                   total_count=total))
           for plan, index in plans.items()]
    st = base()
    return st.model_copy(update={"id": f"st_ornek_{n}", "period_start": start, "period_end": date(2099, n, 25),
                                 "due_date": date(2099, n, 28), "transactions": txs})


def installment_codes(st: Statement, stored) -> dict[str, dict]:
    return {i.code: i.details for i in gate.check(st, stored) if i.code.startswith("installment_")}


def test_out_of_order_upload_three_installments():
    """1/3, 2/3, 3/3 on three statements in a row, uploaded as 1/3, 3/3, 2/3: no false alarm."""
    first, second, third = (cycle(n, {"pl_ornek": n}) for n in (1, 2, 3))
    assert installment_codes(third, [first]) == {}  # two cycles after 1/3 comes 3/3
    assert installment_codes(second, [first, third]) == {}


def test_out_of_order_upload_still_flags_a_wrong_number():
    first, third = cycle(1, {"pl_ornek": 1}), cycle(3, {"pl_ornek": 2})
    found = installment_codes(third, [first])
    assert found["installment_sequence"]["plan_ids"] == ["pl_ornek"]
    assert found["installment_sequence"]["cycles"] == 2


def test_missing_counts_the_cycles_between():
    """A plan that ended in the statement not stored is not missing; one that should still run is."""
    first = cycle(1, {"pl_ornek_kisa": 2, "pl_ornek_uzun": 1}, total=3)  # short plan ends at 3/3 in 2099-02
    third = cycle(3, {}, total=3)
    found = installment_codes(third, [first])
    assert found["installment_missing"]["plan_ids"] == ["pl_ornek_uzun"]
    assert found["installment_missing"]["cycles"] == 2
    assert "pl_ornek_kisa" in installment_codes(cycle(2, {}), [first])["installment_missing"]["plan_ids"]


def test_cut_off_moved_by_a_holiday_is_one_cycle():
    first = cycle(1, {"pl_ornek": 1})
    second = cycle(2, {"pl_ornek": 2}).model_copy(update={"period_end": date(2099, 2, 28)})  # moved three days
    assert installment_codes(second, [first]) == {}


def test_previous_too_far_back_is_not_compared():
    first, fifth = cycle(1, {"pl_ornek": 1}, total=9), cycle(5, {"pl_ornek": 9}, total=9)
    assert installment_codes(fifth, [first]) == {}
