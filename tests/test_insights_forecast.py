"""Next statement forecast and its backtest, on five invented monthly statements."""

from __future__ import annotations

from datetime import date

from kalan.insights.forecast import backtest, forecast_month, next_statement_forecast, open_period
from kalan.models import FixedCost, InstallmentInfo, Transaction
from tests.insights_data import statement, tx

SHOPS = ["ORNEK MARKET", "ORNEK FIRIN", "ORNEK KITAP", "ORNEK SPOR", "ORNEK CICEK"]


def months():
    out = []
    for i in range(5):
        m = i + 1
        info = InstallmentInfo(remaining_count=6 - m, remaining_amount_each=10000, is_last=m == 6, plan_id="pl_x",
                               index=m, total_count=6)
        out.append(statement(f"st_{m}", period_end=date(2099, m, 15), transactions=[
            tx(f"i{m}", date(2099, 1, 2), 10000, kind="installment", description=f"ORNEK ELEKTRONIK {m}/6",
               installment=info),
            tx(f"s{m}", date(2099, m, 3), 4999, description="ORNEK MUZIK"),
            tx(f"v{m}", date(2099, m, 5), 10000 * m, description=SHOPS[i]),
            tx(f"p{m}", date(2099, m, 6), 99999, kind="payment", direction="credit", description="ODEME"),
        ]))
    return out


RENT = FixedCost(id="fc_1", name="ORNEK KIRA", amount=100000, day=1, start="2099-01")


def test_forecast_hand_checked():
    """June: installment 6/6 = 100,00; subscription 49,99; variable median of 100..500 = 300,00; rent 1.000,00."""
    f = forecast_month("2099-06", months(), [], "TRY", fixed_costs=[RENT])
    assert f["parts"] == {"installments": 10000, "subscriptions": 4999, "variable": 30000, "fixed": 100000}
    assert f["total"] == 144999
    assert f["variable_basis"]["months"] == ["2099-01", "2099-02", "2099-03", "2099-04", "2099-05"]
    without = forecast_month("2099-06", months(), [], "TRY", fixed_costs=[RENT], include_fixed=False)
    assert without["total"] == 44999


def test_backtest_hand_checked():
    """Without fixed costs: March 299,99 vs 449,99; April 349,99 vs 549,99; May 399,99 vs 649,99.

    Errors -150,00 / -200,00 / -250,00: MAE 200,00, bias -200,00,
    MAPE = (150/449,99 + 200/549,99 + 250/649,99) / 3 = (0.3333 + 0.3636 + 0.3846) / 3 = 0.36.
    """
    bt = backtest(months(), [], "TRY", include_fixed=False)
    assert [(r["month"], r["forecast"], r["actual"]) for r in bt["months"]] == [
        ("2099-03", 29999, 44999), ("2099-04", 34999, 54999), ("2099-05", 39999, 64999)]
    assert (bt["count"], bt["mae"], bt["bias"], bt["mape"]) == (3, 20000, -20000, "0.36")
    assert backtest(months()[:2], [], "TRY")["count"] == 0


def test_open_period_and_next():
    manual = Transaction(id="m1", date=date(2099, 5, 20), description="ORNEK KAFE", amount=35000,
                         direction="debit", kind="purchase", source="manual")
    old = manual.model_copy(update={"id": "m0", "date": date(2099, 5, 10)})
    op = open_period(months(), [manual, old], today=date(2099, 5, 25))
    assert op["from"] == "2099-05-16" and [t["id"] for t in op["transactions"]] == ["m1"] and op["debit_total"] == 35000
    nxt = next_statement_forecast(months(), [manual], "TRY", include_fixed=False, today=date(2099, 5, 25))
    assert nxt["forecast"]["month"] == "2099-06"
    # the known manual row (350,00, dated in May) is attributed to May by date, not to the June statement
    assert nxt["backtest"]["count"] == 3 and "months" not in nxt["backtest"]


def test_routes(client, store):
    for st in months():
        store.add_statement(st)
    body = client.get("/insights/forecast", params={"include_fixed": "false"}).json()
    assert body["forecast"]["month"] == "2099-06" and body["forecast"]["total"] == 44999
    bt = client.get("/insights/forecast/backtest", params={"include_fixed": "false"}).json()
    assert bt["mape"] == "0.36" and len(bt["months"]) == 3
