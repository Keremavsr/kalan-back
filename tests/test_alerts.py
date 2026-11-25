"""Alerts computed in code, on the synthetic VakıfBank statements and invented variations."""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

from kalan.alerts import compute_alerts, latest_per_card
from kalan.pipeline import parse_pdf
from kalan.schedule import build_schedule
from kalan.summary import subscriptions
from tests.conftest import VB_PDFS, upload


@cache
def statements():
    return tuple(parse_pdf(p) for p in VB_PDFS)


def alerts_for(sts, today=None, subs=None):
    sts = list(sts)
    return compute_alerts(sts, build_schedule(sts), subscriptions(sts, []) if subs is None else subs,
                          today=today or date(2000, 1, 1))


def kinds(alerts):
    return sorted(a.kind for a in alerts)


def test_charges_and_installment_ending():
    al = alerts_for(statements()[:10])
    assert kinds(al) == ["fee_charged", "installment_ending", "interest_charged"]
    interest = next(a for a in al if a.kind == "interest_charged")
    st = statements()[9]
    assert interest.month == st.cut_off_month and interest.refs.statement_ids == [st.id]
    assert interest.params["total"] == sum(t.amount for t in st.transactions if t.kind == "interest")
    ending = next(a for a in al if a.kind == "installment_ending")
    assert ending.refs.plan_ids and ending.message_key == "alert.installment_ending"


def test_missing_month():
    sts = statements()
    al = alerts_for(sts[:3] + sts[4:6])
    gap = next(a for a in al if a.kind == "missing_statement")
    assert gap.month == sts[3].cut_off_month and gap.severity == "warning"


def test_limit_due_soon_foreign():
    st = statements()[11]
    high = st.model_copy(update={"statement_total": int(st.credit_limit * 0.9)})
    al = alerts_for([high], today=high.due_date - timedelta(days=3))
    by = {a.kind: a for a in al}
    assert by["limit_high"].severity == "warning" and by["limit_high"].params["used_share"] == "0.90"
    assert by["due_soon"].params["days_left"] == 3 and by["due_soon"].severity == "warning"
    over = high.model_copy(update={"statement_total": st.credit_limit + 1})
    al = {a.kind: a for a in alerts_for([over], today=over.due_date)}
    assert al["limit_high"].severity == "critical" and al["due_soon"].severity == "critical"
    again = alerts_for([over], today=over.due_date)
    assert [a.severity for a in again][:2] == ["critical", "critical"]  # critical first
    assert al["limit_high"].id == next(a.id for a in again if a.kind == "limit_high")  # stable id
    foreign = next(s for s in statements() if any(t.original_currency for t in s.transactions))
    fa = next(a for a in alerts_for([foreign]) if a.kind == "foreign_currency")
    assert fa.params["currencies"] and fa.refs.transaction_ids


def test_no_due_alert_after_due_date_or_zero_balance():
    st = statements()[11]
    assert "due_soon" not in kinds(alerts_for([st], today=st.due_date + timedelta(days=1)))
    zero = st.model_copy(update={"statement_total": 0})
    assert "due_soon" not in kinds(alerts_for([zero], today=zero.due_date))


def test_subscription_price_rise():
    subs = [{"merchant": "DEMO STREAM", "amount_last": 12000, "amount_previous": 10000, "currency": "TRY",
             "months_seen": ["2026-08", "2026-09"], "price_change": "0.20"},
            {"merchant": "FLAT", "amount_last": 100, "amount_previous": 100, "currency": "TRY",
             "months_seen": ["2026-08", "2026-09"], "price_change": "0.00"}]
    al = [a for a in alerts_for([], subs=subs) if a.kind == "subscription_price_rise"]
    assert len(al) == 1 and al[0].month == "2026-09" and al[0].params["price_change"] == "0.20"


def test_fixture_price_rise_detected():
    sts = statements()
    assert any("subscription_price_rise" in kinds(alerts_for(sts[:n])) for n in range(2, len(sts) + 1))


def test_latest_per_card():
    assert latest_per_card(list(statements()))[0].id == statements()[-1].id


def test_api_alerts(client):
    for p in VB_PDFS[8:10]:
        assert upload(client, p).status_code == 201
    items = client.get("/alerts").json()["items"]
    assert items and set(items[0]) == {"id", "kind", "severity", "month", "message_key", "params", "refs"}
    assert set(items[0]["refs"]) == {"statement_ids", "transaction_ids", "plan_ids"}
    assert len({a["id"] for a in items}) == len(items)
