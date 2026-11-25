"""What-if installment simulator (code only)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kalan.schedule import Schedule, ScheduleMonth
from kalan.simulate import SimulationIn, installments, simulate
from tests.conftest import VB_PDFS, upload


def test_plain_split_puts_remainder_first():
    parts = installments(100001, 3, None)
    assert [p["amount"] for p in parts] == [33335, 33333, 33333]
    assert sum(p["amount"] for p in parts) == 100001 and all(p["interest"] == 0 for p in parts)


def test_annuity_matches_docstring_example():
    parts = installments(100000, 3, "3")
    assert [p["amount"] for p in parts] == [35353, 35353, 35353]
    assert [p["interest"] for p in parts] == [3000, 2029, 1030]
    assert sum(p["principal"] for p in parts) == 100000


@pytest.mark.parametrize(("amount", "count", "rate"), [(123457, 7, "3.11"), (5000, 12, "0.5"), (99, 9, "4.25")])
def test_annuity_principal_always_sums_to_amount(amount, count, rate):
    parts = installments(amount, count, rate)
    assert sum(p["principal"] for p in parts) == amount
    assert all(p["amount"] == p["principal"] + p["interest"] for p in parts)


def baseline() -> Schedule:
    months = [ScheduleMonth(month=m, total=t) for m, t in (("2026-12", 50000), ("2027-01", 20000), ("2027-02", 0))]
    return Schedule(currency="TRY", start_month="2026-12", months=months, plans=[])


def test_simulate_with_and_without():
    out = simulate(SimulationIn(amount=60000, count=3, start_month="2027-01"), baseline())
    rows = {r["month"]: r for r in out["months"]}
    assert list(rows) == ["2026-12", "2027-01", "2027-02", "2027-03"]
    assert rows["2027-01"] == {"month": "2027-01", "without": 20000, "purchase": 20000, "with": 40000}
    assert rows["2027-03"]["with"] == 20000
    assert out["peak"] == {"without": {"month": "2026-12", "total": 50000},
                           "with": {"month": "2026-12", "total": 50000}}
    assert out["purchase"]["total_cost"] == 60000 and out["purchase"]["ends"] == "2027-03"


def test_simulate_interest_moves_peak():
    out = simulate(SimulationIn(amount=100000, count=3, start_month="2026-12", interest_monthly="3"), baseline())
    assert out["purchase"]["interest_total"] == 6059
    assert out["peak"]["with"] == {"month": "2026-12", "total": 85353}


def test_validation():
    for bad in ({"amount": 0, "count": 3, "start_month": "2026-12"},
                {"amount": 10, "count": 0, "start_month": "2026-12"},
                {"amount": 10, "count": 3, "start_month": "2026-13"},
                {"amount": 10, "count": 3, "start_month": "2026-12", "interest_monthly": "3,1"}):
        with pytest.raises(ValidationError):
            SimulationIn.model_validate(bad)


def test_api_simulate_on_stored_statements(client):
    assert upload(client, VB_PDFS[9]).status_code == 201
    sched = client.get("/installments/schedule").json()
    body_in = {"amount": 120000, "count": 4, "start_month": sched["start_month"]}
    resp = client.post("/simulate/installment", json=body_in)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    first = body["months"][0]
    assert first["without"] == sched["months"][0]["total"] and first["with"] == first["without"] + 30000
    assert client.post("/simulate/installment", json={"amount": 1, "count": 2}).status_code == 422
