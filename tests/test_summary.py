from __future__ import annotations

from datetime import date

from kalan.models import InstallmentInfo, Statement, Transaction
from kalan.summary import monthly_summary, ratio, subscriptions

_n = iter(range(1, 10_000))


def tx(d: str, desc: str, amount: int, direction: str = "debit", kind: str = "purchase", **kw) -> Transaction:
    return Transaction(id=f"tx_{next(_n):06d}", date=date.fromisoformat(d), description=desc, amount=amount,
                       direction=direction, kind=kind, source="manual", **kw)


def statement(end: str, *txs: Transaction) -> Statement:
    e = date.fromisoformat(end)
    return Statement(id=f"st_{end}", bank="b", product="p", card_last4="0000", period_start=e.replace(day=1),
                     period_end=e, due_date=e, statement_total=0, minimum_payment=0,
                     transactions=[t.model_copy(update={"source": "statement"}) for t in txs])


def test_ratio():
    assert ratio(18, 100) == "0.18" and ratio(1, 3) == "0.33" and ratio(2, 3) == "0.67"
    assert ratio(-12, 100) == "-0.12" and ratio(5, 0) is None


def test_monthly_summary_rules():
    st = statement(
        "2026-09-23",
        tx("2026-08-25", "ONCEKI BAKIYE", 50000, kind="carry_over"),
        tx("2026-08-30", "MARKET", 30000, category="groceries"),  # August date, September statement
        tx("2026-09-02", "ODEME", 50000, "credit", "payment"),
        tx("2026-09-05", "IADE", 2000, "credit", "refund"),
        tx("2026-09-10", "KAFE", 10000, category="dining"),
        tx("2026-09-11", "YURT DISI", 999, currency="USD"),
    )
    manual = [tx("2026-09-15", "PAZAR", 10000)]
    s = monthly_summary([st], manual, "2026-09", "TRY")
    assert s["total_spend"] == 50000 and s["total_credit"] == 2000
    assert s["by_category"] == [
        {"category": "groceries", "amount": 30000, "share": "0.60"},
        {"category": "dining", "amount": 10000, "share": "0.20"},
        {"category": "uncategorized", "amount": 10000, "share": "0.20"},
    ]
    assert s["statement_ids"] == ["st_2026-09-23"] and s["previous_month"] is None
    nxt = monthly_summary([st], manual, "2026-10", "TRY")
    assert nxt["total_spend"] == 0 and nxt["previous_month"] == {"total_spend": 50000, "change": "-1.00"}


def test_previous_month_with_zero_spend_has_null_change():
    st = statement("2026-08-23", tx("2026-08-02", "ODEME", 100, "credit", "payment"))
    s = monthly_summary([st], [tx("2026-09-01", "X", 100)], "2026-09", "TRY")
    assert s["previous_month"] == {"total_spend": 0, "change": None}


def test_subscription_needs_two_consecutive_months_within_25_percent():
    txs = [tx("2026-06-03", "VIDEO", 10000), tx("2026-07-03", "VIDEO", 10000),  # run 1
           tx("2026-09-03", "VIDEO", 12500),  # gap in August: starts a new run of one
           tx("2026-06-05", "ONCE", 5000),
           tx("2026-07-09", "JUMP", 1000), tx("2026-08-09", "JUMP", 1300)]  # +30%: not recurring
    items = {i["merchant"]: i for i in subscriptions([], txs)}
    assert set(items) == {"VIDEO"}
    assert items["VIDEO"]["months_seen"] == ["2026-06", "2026-07"]
    assert items["VIDEO"]["price_change"] == "0.00"


def test_subscription_ignores_installments_fees_and_busy_merchants():
    plan = InstallmentInfo(remaining_count=2, remaining_amount_each=100, is_last=False, plan_id="pl_1")
    txs = [tx("2026-07-01", "TAKSIT", 100, kind="installment", installment=plan),
           tx("2026-08-01", "TAKSIT", 100, kind="installment", installment=plan),
           tx("2026-07-02", "UCRET", 50, kind="fee"), tx("2026-08-02", "UCRET", 50, kind="fee"),
           tx("2026-07-03", "MARKET", 900), tx("2026-07-20", "MARKET", 950), tx("2026-08-03", "MARKET", 900)]
    assert subscriptions([], txs) == []


def test_model_flag_makes_a_single_charge_a_subscription():
    t = tx("2026-09-03", "BULUT DEPOLAMA", 4999)
    assert subscriptions([], [t]) == []
    assert subscriptions([], [t], {t.id}) == [{
        "merchant": "BULUT DEPOLAMA", "amount_last": 4999, "amount_previous": None, "currency": "TRY",
        "months_seen": ["2026-09"], "price_change": None}]


def test_subscription_uses_statement_month():
    a = statement("2026-08-23", tx("2026-08-20", "MUZIK", 5999))
    b = statement("2026-09-23", tx("2026-09-21", "MUZIK", 5999))
    [item] = subscriptions([a, b], [])
    assert item["months_seen"] == ["2026-08", "2026-09"] and item["amount_previous"] == 5999
