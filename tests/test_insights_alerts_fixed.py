"""New alerts (charged after cancel, statement overdue) and fixed-cost matching tolerance."""

from __future__ import annotations

from datetime import date

from kalan.alerts import compute_alerts
from kalan.insights.fixed_match import duplicate_fixed_costs, fixed_cost_report, match_fixed_costs
from kalan.insights.forecast import forecast_month
from kalan.models import FixedCost
from kalan.schedule import build_schedule
from tests.insights_data import statement, tx


def alerts(sts, today, cancelled=None):
    return compute_alerts(sts, build_schedule(sts), [], today=today, cancelled=cancelled)


def test_charged_after_cancel():
    st = statement(period_end=date(2099, 3, 15), transactions=[
        tx("a", date(2099, 2, 20), 4999, description="ORNEK MUZIK"),
        tx("b", date(2099, 3, 3), 4999, description="Ornek Muzik"),
        tx("c", date(2099, 3, 4), 9999, description="ORNEK MARKET"),
    ])
    found = [a for a in alerts([st], date(2099, 3, 16), [{"merchant": "ORNEK MÜZIK", "cancelled_on": "2099-02-28"}])
             if a.kind == "charged_after_cancel"]
    assert len(found) == 1
    a = found[0]
    assert a.severity == "critical" and a.refs.transaction_ids == ["b"] and a.params["total"] == 4999
    assert a.month == "2099-03" and a.message_key == "alert.charged_after_cancel"
    none = alerts([st], date(2099, 3, 16), [{"merchant": "ORNEK MUZIK", "cancelled_on": "2099-03-10"}])
    assert "charged_after_cancel" not in {x.kind for x in none}


def test_statement_overdue():
    st = statement(period_end=date(2099, 1, 31), due=date(2099, 2, 10))
    # next cut-off 2099-02-28 (clipped); grace 7 days
    assert "statement_overdue" not in {a.kind for a in alerts([st], date(2099, 3, 7))}
    late = next(a for a in alerts([st], date(2099, 3, 8)) if a.kind == "statement_overdue")
    assert late.params == {"bank": "ornekbank", "card_last4": "0000", "expected_cut_off": "2099-02-28",
                           "days_overdue": 8}
    assert late.month == "2099-02" and late.severity == "warning"


RENT = FixedCost(id="fc_rent", name="ORNEK KIRA", amount=1000000, day=31, start="2099-01")
GYM = FixedCost(id="fc_gym", name="ORNEK SPOR", amount=50000, day=5, start="2099-01", end="2099-12")


def test_matching_tolerance():
    """Rent 10.000,00 on day 31: 2099-02-28 (clipped) row 10.700,00 is within 7,5 % (+7 %) and 0 days.

    A 2099-04-02 row of 9.300,00 (-7 %) is 2 days after 2099-03-31: matches March across the month end.
    A 2099-05-03 row (3 days after 2099-04-30) and a 10.800,00 row (+8 %) do not match.
    """
    rows = [
        tx("r2", date(2099, 2, 28), 1070000), tx("r4", date(2099, 4, 2), 930000),
        tx("r5", date(2099, 5, 3), 1000000), tx("r6", date(2099, 6, 30), 1080000),
        tx("g1", date(2099, 2, 6), 50000), tx("g2", date(2099, 2, 7), 50000),
    ]
    links = match_fixed_costs([RENT, GYM], rows)
    assert [(lk["fixed_cost_id"], lk["month"], lk["transaction_id"], lk["day_diff"]) for lk in links] == [
        ("fc_gym", "2099-02", "g1", 1), ("fc_rent", "2099-02", "r2", 0), ("fc_rent", "2099-03", "r4", 2)]
    assert links[1]["amount_diff"] == 70000


def test_duplicates_and_report():
    twin = FixedCost(id="fc_twin", name="ornek kira", amount=1040000, day=1, start="2099-06")
    other = FixedCost(id="fc_x", name="ORNEK X", amount=1000000, day=15, start="2099-01")
    dups = duplicate_fixed_costs([RENT, twin, other])
    assert dups == [{"fixed_cost_ids": ["fc_rent", "fc_twin"], "same_name": True, "amount_diff": 40000, "day_gap": 1}]
    st = statement(period_end=date(2099, 4, 15), transactions=[
        tx("x1", date(2099, 1, 31), 1000000), tx("x3", date(2099, 3, 31), 1000000), tx("x4", date(2099, 4, 10), 1)])
    rep = fixed_cost_report([RENT], [st], [])
    assert [m["month"] for m in rep["costs"][0]["matches"]] == ["2099-01", "2099-03"]
    assert rep["costs"][0]["missing_months"] == ["2099-02"]


def test_forecast_does_not_count_matched_fixed_cost_twice():
    sts = [statement(f"st_{m}", period_end=date(2099, m, 15), transactions=[
        tx(f"k{m}", date(2099, m, 5), 50000, description="ORNEK SPOR SALONU"),
        tx(f"v{m}", date(2099, m, 9), 10000 * m, description=f"ORNEK DUKKAN {'ABC'[m - 1]}"),
    ]) for m in (1, 2, 3)]
    ids = {"k1", "k2", "k3"}
    f = forecast_month("2099-04", sts, [], "TRY", fixed_costs=[GYM], fixed_tx_ids=ids)
    # GYM is also seen as a subscription (same amount every month), so the matched rows count once, as fixed
    assert f["parts"]["fixed"] == 50000 and f["variable_basis"]["median"] == 20000


def test_routes(client, store):
    store.add_statement(statement(period_end=date(2099, 3, 15), transactions=[
        tx("b", date(2099, 3, 3), 4999, description="ORNEK MUZIK")]))
    r = client.post("/insights/cancelled-subscriptions", json={"merchant": "ORNEK MUZIK", "cancelled_on": "2099-03-01"})
    assert r.status_code == 201 and r.json()["items"] == [{"merchant": "ORNEK MUZIK", "cancelled_on": "2099-03-01"}]
    kinds = {a["kind"] for a in client.get("/alerts").json()["items"]}
    assert "charged_after_cancel" in kinds
    assert client.delete("/insights/cancelled-subscriptions", params={"merchant": "ornek muzik"}).status_code == 204
    assert client.delete("/insights/cancelled-subscriptions", params={"merchant": "ornek muzik"}).status_code == 404
    assert client.get("/insights/cancelled-subscriptions").json() == {"items": []}
    client.post("/fixed-costs", json={"name": "ORNEK SPOR", "amount": 4999, "day": 3, "start": "2099-01"})
    rep = client.get("/insights/fixed-costs/matches").json()
    assert rep["tolerance"] == {"amount": "0.075", "days": 2} and rep["costs"][0]["matches"][0]["transaction_id"] == "b"
