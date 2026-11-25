"""Pay-only-the-minimum plan, with invented numbers."""

from __future__ import annotations

from datetime import date

from kalan.insights.minimum import minimum_payment_plan
from kalan.insights.rules import RuleOverrides, contractual_rate, max_contractual_rate, minimum_ratio_for_limit
from tests.insights_data import statement


def test_hand_checked_first_cycles():
    """1.000,00 TL debt, printed minimum 200,00, rate 2,00 % a month, BSMV and KKDF 15 % each.

    Cut-off 2099-01-15, next 2099-02-15 (31 days).
    Cycle 1: pay 200,00; unpaid 800,00; interest = 800,00 * 2 % * 31 / 30 = 16,5333 -> 16,53;
    BSMV = KKDF = 16,53 * 15 % = 2,4795 -> 2,48; next total = 800,00 + 16,53 + 2,48 + 2,48 = 821,49.
    Cycle 2 (2099-02-15 to 2099-03-15, 28 days): pay 20 % of 821,49 = 164,298 -> 164,30 (rounded up);
    unpaid 657,19; interest = 657,19 * 2 % * 28 / 30 = 12,2675 -> 12,27; BSMV = KKDF = 1,8405 -> 1,84;
    next total = 657,19 + 12,27 + 1,84 + 1,84 = 673,14.
    """
    st = statement(total=100000, minimum=20000, limit=5000000)
    plan = minimum_payment_plan(st, RuleOverrides(contractual_monthly="2.00"))
    c1, c2 = plan["cycles"][:2]
    assert (c1["payment"], c1["unpaid"], c1["days"], c1["interest"], c1["bsmv"], c1["kkdf"], c1["next_balance"]) == (
        20000, 80000, 31, 1653, 248, 248, 82149)
    assert (c2["payment"], c2["unpaid"], c2["days"], c2["interest"], c2["bsmv"], c2["kkdf"], c2["next_balance"]) == (
        16430, 65719, 28, 1227, 184, 184, 67314)
    mo = plan["minimum_only"]
    assert mo["paid_off"] and mo["balance_left"] == 0
    assert mo["total_paid"] == 100000 + mo["cost"]
    assert mo["cost"] == mo["interest"] + mo["bsmv"] + mo["kkdf"] > 0
    assert plan["pay_in_full"] == {"total_paid": 100000, "cost": 0}
    assert plan["extra_cost"] == mo["cost"]
    assert mo["cycles"] == len(plan["cycles"]) > 12
    assert plan["rates"]["contractual_monthly"][0]["source"] == "override"


def test_bank_example_document():
    """The bank's public example: 1.000 TL, 20 % paid, 800 TL unpaid at 1,98 % over 30 days = 15,84 TL."""
    st = statement(period_end=date(2099, 4, 10), next_period_end=date(2099, 5, 10), minimum=20000)
    plan = minimum_payment_plan(st, RuleOverrides(contractual_monthly="1.98", bsmv="0", kkdf="0"))
    assert plan["cycles"][0]["interest"] == 1584


def test_caps_tiers_and_printed_rate():
    on = date(2099, 1, 1)  # the newest rule set applies
    assert max_contractual_rate(2999999, on).value == "3.08"
    assert max_contractual_rate(3000000, on).value == "3.58"
    assert max_contractual_rate(18000000, on).value == "4.08"
    assert max_contractual_rate(100, date(2026, 11, 30)).value == "3.25"
    assert contractual_rate(100, on, "2.50", RuleOverrides()).source == "statement"
    capped = contractual_rate(100, on, "9.99", RuleOverrides())
    assert capped.value == "3.08" and capped.source == "rules"
    assert minimum_ratio_for_limit(10000000, on).value == "0.20"
    assert minimum_ratio_for_limit(10000001, on).value == "0.30"
    assert minimum_ratio_for_limit(20000001, on).value == "0.40"


def test_ratio_without_limit_uses_printed_minimum():
    st = statement(total=100000, minimum=40000, limit=None)
    plan = minimum_payment_plan(st)
    assert plan["rates"]["minimum_ratio"]["value"] == "0.40"
    assert plan["rates"]["minimum_ratio"]["source"] == "statement"


def test_installments_added_and_zero_debt():
    st = statement(total=100000, minimum=20000)
    plan = minimum_payment_plan(st, RuleOverrides(contractual_monthly="2.00"), {"2099-02": 50000})
    assert plan["cycles"][0]["added"] == 50000
    assert plan["pay_in_full"]["total_paid"] == 150000
    assert any(a["key"] == "scheduled_installments_added" for a in plan["assumptions"])
    empty = minimum_payment_plan(statement(total=0, minimum=0))
    assert empty["cycles"] == [] and empty["minimum_only"]["paid_off"] and empty["extra_cost"] == 0


def test_route(client, store):
    assert client.get("/insights/minimum-payment").status_code == 409
    store.add_statement(statement())
    body = client.get("/insights/minimum-payment", params={"cycles": 3}).json()
    assert len(body["cycles"]) == 3 and body["minimum_only"]["paid_off"]
    r = client.put("/insights/rules", json={"contractual_monthly": "1.50"})
    assert r.status_code == 200 and r.json()["overrides"]["contractual_monthly"] == "1.50"
    again = client.get("/insights/minimum-payment").json()
    assert again["rates"]["contractual_monthly"][0] == {"from_cycle": 1, "monthly": "1.50", "source": "override",
                                                        "detail": None}
    assert client.put("/insights/rules", json={"bsmv": "2"}).status_code == 422
    assert client.get("/insights/minimum-payment", params={"statement_id": "nope"}).status_code == 404
