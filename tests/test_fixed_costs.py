"""Recurring fixed costs: a separate schedule series, never mixed with card installments."""

from __future__ import annotations

from kalan.models import FixedCost
from kalan.schedule import build_schedule, fixed_series
from tests.conftest import VB_PDFS, upload


def cost(**kw) -> FixedCost:
    base = {"id": "fc_1", "name": "Rent", "amount": 1500000, "currency": "TRY", "day": 31, "start": "2026-01"}
    return FixedCost(**(base | kw))


def test_series_respects_start_end_and_short_months():
    series = fixed_series([cost(end="2026-03"), cost(id="fc_2", name="Dues", amount=20000, day=5, start="2026-02")],
                          ["2026-01", "2026-02", "2026-03", "2026-04"], "TRY")
    totals = [(m.month, m.total, [i.fixed_cost_id for i in m.items]) for m in series.months]
    assert totals == [("2026-01", 1500000, ["fc_1"]), ("2026-02", 1520000, ["fc_2", "fc_1"]),
                      ("2026-03", 1520000, ["fc_2", "fc_1"]), ("2026-04", 20000, ["fc_2"])]
    assert series.months[1].items[1].date.isoformat() == "2026-02-28"
    assert series.kind == "fixed"


def test_other_currency_not_in_total():
    series = fixed_series([cost(currency="USD")], ["2026-05"], "TRY")
    assert series.months[0].total == 0 and series.months[0].items[0].currency == "USD"


def test_schedule_keeps_installments_separate():
    sched = build_schedule([], start_month="2026-01", months=2, fixed_costs=[cost()])
    assert [m.total for m in sched.months] == [0, 0]
    assert [m.total for m in sched.fixed.months] == [1500000, 1500000]


def test_api_fixed_costs(client):
    assert upload(client, VB_PDFS[9]).status_code == 201
    before = client.get("/installments/schedule").json()
    start = before["start_month"]
    resp = client.post("/fixed-costs", json={"name": "Site aidatı", "amount": 250000, "day": 10, "start": start})
    assert resp.status_code == 201, resp.text
    fc = resp.json()
    assert fc["currency"] == "TRY" and fc["end"] is None and fc["id"].startswith("fc_")
    after = client.get("/installments/schedule").json()
    assert [m["total"] for m in after["months"]] == [m["total"] for m in before["months"]]
    assert after["fixed"]["kind"] == "fixed" and all(m["total"] == 250000 for m in after["fixed"]["months"])
    assert client.get("/fixed-costs").json()["items"] == [fc]
    bad = client.post("/fixed-costs", json={"name": "x", "amount": 1, "day": 1, "start": "2026-05", "end": "2026-04"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_period"
    for invalid in ({"amount": 0, "day": 1}, {"amount": 1, "day": 32}):
        assert client.post("/fixed-costs", json={"name": "x", "start": "2026-05", **invalid}).status_code == 422
    assert client.delete(f"/fixed-costs/{fc['id']}").status_code == 204
    assert client.delete(f"/fixed-costs/{fc['id']}").status_code == 404
    assert client.get("/fixed-costs").json()["items"] == []


def test_fixed_cost_name_is_redacted(client):
    body = {"name": "Kira TR33 0006 1005 1978 6457 8413 26", "amount": 100, "day": 1, "start": "2026-05"}
    assert "TR33" not in client.post("/fixed-costs", json=body).json()["name"]
