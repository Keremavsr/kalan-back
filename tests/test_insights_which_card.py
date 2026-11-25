"""Which card pushes the first installment furthest (invented cards and dates)."""

from __future__ import annotations

from datetime import date

from kalan.insights.dates import next_business_day, shift_month
from kalan.insights.which_card import card_options
from tests.insights_data import statement


def cards():
    return [
        statement("st_a", bank="ornekbank", last4="1111", period_end=date(2099, 1, 15), due=date(2099, 1, 25)),
        statement("st_b", bank="digerbank", last4="2222", period_end=date(2099, 1, 5), due=date(2099, 1, 15)),
    ]


def test_best_card_and_weekend_shift():
    out = card_options(cards(), today=date(2099, 1, 16))
    a, b = out["cards"]
    assert out["best"]["card_last4"] == "1111"
    assert (a["cut_off"], a["due_date"], a["due_date_effective"], a["interest_free_days"]) == (
        "2099-02-15", "2099-02-25", "2099-02-25", 40)
    assert a["first_installment_month"] == "2099-02" and a["basis"] == "projected"
    # 2099-02-15 is a Sunday: paid on Monday 2099-02-16
    assert (b["due_date"], b["due_date_effective"], b["shifted_days"], b["interest_free_days"]) == (
        "2099-02-15", "2099-02-16", 1, 31)
    assert any(x["key"] == "holidays_unknown" for x in out["assumptions"])


def test_month_end_and_fixed_holiday():
    end = statement("st_c", period_end=date(2099, 1, 31), due=date(2099, 2, 10))
    c = card_options([end], today=date(2099, 2, 10))["best"]
    assert (c["cut_off"], c["due_date"]) == ("2099-02-28", "2099-03-10")
    hol = statement("st_d", period_end=date(2099, 10, 19), due=date(2099, 10, 29))
    d = card_options([hol], today=date(2099, 10, 19))["best"]
    assert d["basis"] == "printed" and d["due_date_effective"] == "2099-10-30" and d["interest_free_days"] == 11


def test_printed_next_dates_and_empty():
    st = statement("st_e", period_end=date(2099, 1, 15), due=date(2099, 1, 25), next_period_end=date(2099, 2, 13))
    st = st.model_copy(update={"next_due_date": date(2099, 2, 24)})
    e = card_options([st], today=date(2099, 1, 20))["best"]
    assert (e["cut_off"], e["due_date"], e["basis"]) == ("2099-02-13", "2099-02-24", "printed")
    assert card_options([], today=date(2099, 1, 1)) == {"date": "2099-01-01", "best": None, "cards": [],
                                                       "assumptions": card_options([], date(2099, 1, 1))["assumptions"]}


def test_calendar_helpers():
    assert shift_month(date(2099, 1, 31), 1) == date(2099, 2, 28)
    assert shift_month(date(2099, 2, 28), 1, day=31) == date(2099, 3, 31)
    assert next_business_day(date(2026, 5, 27)) == date(2026, 6, 1)  # Kurban Bayramı, then a weekend


def test_route(client, store):
    for st in cards():
        store.add_statement(st)
    body = client.get("/insights/which-card", params={"date": "2099-01-16"}).json()
    assert body["best"]["statement_id"] == "st_a" and len(body["cards"]) == 2
    assert client.get("/insights/which-card", params={"date": "nope"}).status_code == 422
