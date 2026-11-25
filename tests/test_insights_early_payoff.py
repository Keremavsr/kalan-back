"""Early payoff of installment plans (invented cash advance and purchase plans)."""

from __future__ import annotations

from datetime import date

from kalan.insights.early_payoff import closed_early, payoff, payoff_overview
from kalan.models import InstallmentInfo
from kalan.schedule import build_schedule
from tests.insights_data import statement, tx


def info(plan, idx, total, each, component=None, is_last=False):
    return InstallmentInfo(remaining_count=total - idx, remaining_amount_each=each, is_last=is_last, plan_id=plan,
                           index=idx, total_count=total, component=component)


def plans_statement():
    parts = {"principal": 100000, "interest": 3000, "bsmv": 450, "kkdf": 450}
    rows = [tx(f"ca_{role}", date(2099, 1, 2), each, kind="cash_advance", description=f"ORNEK NAKIT 2.{role}",
               installment=info("pl_ca", 2, 6, each, component=role)) for role, each in parts.items()]
    rows.append(tx("p3", date(2099, 1, 4), 20000, kind="installment", description="ORNEK MOBILYA 3/5",
                   installment=info("pl_p", 3, 5, 20000)))
    rows.append(tx("e1", date(2099, 1, 6), 30000, kind="installment", description="ORNEK BEYAZ ESYA 3/6",
                   installment=info("pl_e", 3, 6, 0, is_last=True)))
    return statement("st_p", period_end=date(2099, 1, 15), transactions=rows)


def test_cash_advance_payoff_hand_checked():
    """4 installments of 1.039,00 left (1.000,00 principal + 30,00 vade farkı + 4,50 BSMV + 4,50 KKDF).

    Paying off in February: remaining 4.156,00, principal 4.000,00, so at most 156,00 saved.
    Paying off in March: February is billed as usual (1.039,00), then 3 x 1.039,00 = 3.117,00 in March.
    """
    sched = build_schedule([plans_statement()], months=6)
    feb = payoff(sched, "pl_ca")
    assert (feb["payoff_month"], feb["remaining_count"], feb["remaining_total"], feb["remaining_principal"],
            feb["would_save_if_future_interest_dropped"]) == ("2099-02", 4, 415600, 400000, 15600)
    assert feb["breakdown"] == {"principal": 400000, "interest": 12000, "bsmv": 1800, "kkdf": 1800}
    assert feb["discount_rule"] == "unknown"
    mar = payoff(sched, "pl_ca", "2099-03")
    assert mar["remaining_total"] == 311700
    effect = {e["month"]: e for e in mar["schedule_effect"]}
    assert effect["2099-02"]["with"] == effect["2099-02"]["without"] == 103900 + 20000
    assert effect["2099-03"]["with"] == 20000 + 311700
    assert effect["2099-04"]["with"] == 0 and effect["2099-05"]["with"] == 0


def test_purchase_payoff_and_window():
    sched = build_schedule([plans_statement()], months=1)  # only February in the window
    p = payoff(sched, "pl_p")
    assert (p["remaining_count"], p["remaining_total"], p["remaining_principal"]) == (2, 40000, 40000)
    assert p["would_save_if_future_interest_dropped"] == 0 and p["breakdown"] is None
    assert payoff(sched, "nope") is None


def test_closed_early_and_overview():
    st = plans_statement()
    assert closed_early([st]) == [{"plan_id": "pl_e", "statement_id": "st_p", "month": "2099-01",
                                   "transaction_id": "e1", "index": 3, "total_count": 6}]
    ov = payoff_overview(build_schedule([st], months=6), [st])
    assert {p["plan_id"] for p in ov["plans"]} == {"pl_ca", "pl_p"}


def test_routes(client, store):
    store.add_statement(plans_statement())
    ov = client.get("/insights/early-payoff").json()
    assert len(ov["plans"]) == 2 and ov["closed_early"][0]["plan_id"] == "pl_e"
    one = client.get("/insights/early-payoff/pl_ca", params={"month": "2099-03"}).json()
    assert one["remaining_total"] == 311700
    assert client.get("/insights/early-payoff/nope").status_code == 404
    bad = client.get("/insights/early-payoff/pl_ca", params={"month": "2098-01"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "month_outside_schedule"
