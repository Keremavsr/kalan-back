"""Regular payments (subscription / bill), frequent places and expected amounts. Invented merchants and amounts."""

from __future__ import annotations

from datetime import date

from kalan.alerts import compute_alerts
from kalan.cards import card_id
from kalan.models import Statement, Transaction
from kalan.regular import expected_matches, frequent_places, regular_payments, subscription_id, totals
from kalan.schedule import build_schedule

_n = iter(range(1, 100_000))


def tx(d: str, desc: str, amount: int, category: str | None = None, **kw) -> Transaction:
    return Transaction(id=f"tx_{next(_n):06d}", date=date.fromisoformat(d), description=desc, amount=amount,
                       direction="debit", kind=kw.pop("kind", "purchase"), source="statement", category=category,
                       merchant=kw.pop("merchant", None), **kw)


def statement(end: str, *txs: Transaction, bank: str = "ornekbank", last4: str = "0000") -> Statement:
    e = date.fromisoformat(end)
    return Statement(id=f"st_{bank}_{end}", bank=bank, product="Kart", card_last4=last4,
                     period_start=e.replace(day=1), period_end=e, due_date=e, statement_total=0, minimum_payment=0,
                     transactions=list(txs))


def months(desc: str, amounts: list[int], category: str | None = None, start: int = 1, day: int = 5) -> list[Statement]:
    return [statement(f"2099-{start + i:02d}-25", tx(f"2099-{start + i:02d}-{day:02d}", desc, a, category))
            for i, a in enumerate(amounts)]


def merge(*groups: list[Statement]) -> list[Statement]:
    """One statement per month holding every group's rows of that month."""
    by_end: dict[str, Statement] = {}
    for group in groups:
        for st in group:
            cur = by_end.get(st.id)
            by_end[st.id] = st if cur is None else cur.model_copy(
                update={"transactions": cur.transactions + st.transactions})
    return [by_end[k] for k in sorted(by_end)]


def only(items, kind=None):
    return [i for i in items if kind is None or i["kind"] == kind]


# -- subscriptions --------------------------------------------------------------------------------


def test_known_service_counts_with_any_price_change():
    items = regular_payments(months("ORNEK MUZIK", [4999, 4999, 7999], "subscriptions"), [])
    (item,) = items
    assert item["kind"] == "subscription" and item["months_seen"] == ["2099-01", "2099-02", "2099-03"]
    assert (item["amount_previous"], item["amount_last"], item["price_change"]) == (4999, 7999, "0.60")
    assert item["id"] == subscription_id("ORNEK MUZIK", "TRY") and item["monthly_amount"] == 7999
    assert item["card_id"] == card_id("ornekbank", "0000") and item["last_date"] == "2099-03-05"


def test_steady_amount_without_category_is_a_subscription():
    (item,) = regular_payments(months("ORNEK SPOR KULUBU", [50000, 50000, 51000]), [])
    assert item["kind"] == "subscription" and item["category"] is None


def test_five_percent_is_the_steady_limit():
    assert regular_payments(months("ORNEK KULUP", [100000, 105000]), [])  # exactly 5 %
    assert not regular_payments(months("ORNEK KULUP", [100000, 105100]), [])  # just above, no steady month


def test_one_price_change_next_to_a_steady_month():
    (item,) = regular_payments(months("ORNEK KULUP", [9999, 9999, 11999]), [])
    assert item["price_change"] == "0.20"
    (older,) = regular_payments(months("ORNEK KULUP", [9999, 9999, 19999]), [])  # more than 50 %: run ends
    assert older["months_seen"] == ["2099-01", "2099-02"] and older["amount_last"] == 9999
    two_jumps = regular_payments(months("ORNEK KULUP", [9999, 9999, 11999, 13999]), [])
    assert two_jumps and two_jumps[0]["months_seen"] == ["2099-01", "2099-02", "2099-03"]


def test_varying_amount_is_never_a_subscription():
    assert regular_payments(months("ORNEK DUKKAN", [45000, 61000, 52000]), []) == []


def test_everyday_categories_are_never_regular():
    # one visit a month with nearly the same amount: still a restaurant, a market, a bakery, fuel
    for desc, category in (("ORNEK LOKANTA", "dining"), ("MONEYPAY ORNEK MARKET", "groceries"),
                           ("ORNEK PASTANESI", "dining"), ("ORNEK PETROL", "fuel")):
        assert regular_payments(months(desc, [30000, 30000, 30100], category), []) == [], desc


def test_model_flag_is_ignored_for_everyday_categories():
    sts = months("ORNEK YEMEK", [30000], "dining")
    flagged = {sts[0].transactions[0].id}
    assert regular_payments(sts, [], flagged) == []
    sts = months("ORNEK BULUT", [30000])
    assert only(regular_payments(sts, [], {sts[0].transactions[0].id}), "subscription")


def test_unreadable_merchant_is_never_regular():
    sts = [statement(f"2099-0{m}-25", tx(f"2099-0{m}-05", "ISTANBUL KADIKOY ORNEK CD", 10000,
                                          merchant_unreadable=True)) for m in (1, 2, 3)]
    assert regular_payments(sts, []) == []


# -- bills ----------------------------------------------------------------------------------------


def test_bill_amount_may_change_and_two_charges_add_up():
    sts = months("ORNEK ELEKTRIK DAGITIM", [41000, 78000], "utilities")
    sts.append(statement("2099-03-25", tx("2099-03-05", "ORNEK ELEKTRIK DAGITIM", 30000, "utilities"),
                         tx("2099-03-20", "ORNEK ELEKTRIK DAGITIM", 12000, "utilities")))
    (item,) = regular_payments(sts, [])
    assert item["kind"] == "bill" and item["amount_last"] == 42000 and item["amount_previous"] == 78000
    assert item["price_change"] is None and item["months_seen"] == ["2099-01", "2099-02", "2099-03"]


def test_bill_word_without_category():
    (item,) = regular_payments(months("ORNEK GSM FATURA", [12000, 19000]), [])
    assert item["kind"] == "bill"


def test_bill_needs_two_consecutive_months_and_at_most_two_charges():
    assert regular_payments(months("ORNEK SU IDARESI", [10000]), []) == []
    three = statement("2099-02-25", *(tx("2099-02-0" + str(d), "ORNEK SU IDARESI", 1000, "utilities")
                                      for d in (1, 2, 3)))
    assert regular_payments([months("ORNEK SU IDARESI", [1000], "utilities")[0], three], []) == []


def test_totals_and_sort_order():
    sts = merge(months("ORNEK DEPOLAMA", [2999, 2999], "subscriptions"),
                months("ORNEK DOGALGAZ", [50000, 90000], "utilities"))
    items = regular_payments(sts, [])
    assert [i["kind"] for i in items] == ["subscription", "bill"]
    assert totals(items) == [{"currency": "TRY", "subscription": 2999, "bill": 90000}]


# -- expected amount ------------------------------------------------------------------------------


def test_expected_amount_counts_until_a_charge_shows_it():
    sts = months("ORNEK MUZIK", [5999, 5999], "subscriptions")
    sid = subscription_id("ORNEK MUZIK", "TRY")
    exp = {sid: {"amount": 2999, "since": "2099-02-10"}}
    (item,) = regular_payments(sts, [], expected=exp)
    assert item["expected_amount"] == 2999 and item["monthly_amount"] == 2999 and item["amount_last"] == 5999
    assert totals([item])[0]["subscription"] == 2999

    still_old = sts + months("ORNEK MUZIK", [5999], "subscriptions", start=3)
    cleared: list[str] = []
    (item,) = regular_payments(still_old, [], expected=exp, cleared=cleared)
    assert item["expected_amount"] == 2999 and cleared == []

    shown = sts + months("ORNEK MUZIK", [2990], "subscriptions", start=3)  # within 1 %, at least 1.00
    (item,) = regular_payments(shown, [], expected=exp, cleared=cleared)
    assert item["expected_amount"] is None and item["monthly_amount"] == 2990 and cleared == [sid]


def test_expected_match_tolerance():
    assert expected_matches(2900, 2999) and not expected_matches(2898, 2999)
    assert expected_matches(1_010_000, 1_000_000) and not expected_matches(1_010_001, 1_000_000)


def test_price_rise_alert_only_for_subscriptions_without_expected():
    sts = merge(months("ORNEK MUZIK", [4999, 5999], "subscriptions"),
                months("ORNEK INTERNET", [30000, 40000], "telecom"))
    sched = build_schedule(sts, months=1)
    items = regular_payments(sts, [])
    rises = [a for a in compute_alerts(sts, sched, items, today=date(2099, 3, 1))
             if a.kind == "subscription_price_rise"]
    assert [a.params["merchant"] for a in rises] == ["ORNEK MUZIK"]
    sid = subscription_id("ORNEK MUZIK", "TRY")
    items = regular_payments(sts, [], expected={sid: {"amount": 3999, "since": "2099-02-26"}})
    assert not [a for a in compute_alerts(sts, sched, items, today=date(2099, 3, 1))
                if a.kind == "subscription_price_rise"]


# -- frequent places ------------------------------------------------------------------------------


def test_frequent_place_average_over_the_window():
    sts = [
        statement("2099-01-25", tx("2099-01-03", "ORNEK YEMEK", 30000, "dining"),
                  tx("2099-01-09", "ORNEK YEMEK", 20000, "dining")),
        statement("2099-02-25", tx("2099-02-03", "ORNEK YEMEK", 25001, "dining")),
        statement("2099-03-25", tx("2099-03-03", "ORNEK KITAP", 10000)),
    ]
    found = frequent_places(sts, [], "TRY")
    assert found["window"] == {"months": ["2099-01", "2099-02", "2099-03"], "count": 3}
    (place,) = found["items"]
    assert place == {"merchant": "ORNEK YEMEK", "kind": "frequent_place", "category": "dining", "months_seen": 2,
                     "visits": 3, "total": 75001, "monthly_average": 25000, "last_date": "2099-02-03",
                     "card_ids": [card_id("ornekbank", "0000")]}


def test_frequent_place_thresholds():
    two_visits = [statement("2099-01-25", tx("2099-01-03", "ORNEK KAFE", 1000)),
                  statement("2099-02-25", tx("2099-02-03", "ORNEK KAFE", 1000))]
    assert frequent_places(two_visits, [], "TRY")["items"] == []
    one_month = [statement("2099-01-25", *(tx(f"2099-01-0{d}", "ORNEK KAFE", 1000) for d in (1, 2, 3)))]
    assert frequent_places(one_month, [], "TRY")["items"] == []


def test_regular_payments_are_not_frequent_places():
    sts = months("ORNEK DEPOLAMA", [2999, 2999, 2999], "subscriptions")
    items = regular_payments(sts, [])
    assert frequent_places(sts, [], "TRY", items)["items"] == []
    assert frequent_places(sts, [], "TRY", [])["items"]  # without the list it would count


def test_frequent_window_is_the_latest_months():
    sts = [statement(f"2099-{m:02d}-25", tx(f"2099-{m:02d}-03", "ORNEK MARKET", 10000, "groceries"),
                     tx(f"2099-{m:02d}-13", "ORNEK MARKET", 10000, "groceries")) for m in range(1, 9)]
    found = frequent_places(sts, [], "TRY", months=6)
    assert found["window"]["months"][0] == "2099-03" and found["items"][0]["visits"] == 12
    assert found["items"][0]["monthly_average"] == 20000
