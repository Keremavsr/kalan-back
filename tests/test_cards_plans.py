"""Cards, closed installment plans, per-card forecast and missing statements. Invented data only."""

from __future__ import annotations

from datetime import date

from kalan.alerts import compute_alerts
from kalan.cards import card_id, label, list_cards
from kalan.insights.forecast import card_rows, next_statement_forecast
from kalan.models import InstallmentInfo, Statement, Transaction
from kalan.regular import regular_payments
from kalan.schedule import build_schedule
from kalan.texts import money, month_range

_n = iter(range(1, 100_000))


def tx(d: str, desc: str, amount: int, kind: str = "purchase", installment: InstallmentInfo | None = None,
       category: str | None = None) -> Transaction:
    return Transaction(id=f"tx_{next(_n):06d}", date=date.fromisoformat(d), description=desc, amount=amount,
                       direction="debit", kind=kind, installment=installment, category=category)


def plan_row(d: str, plan: str, index: int, total: int, each: int) -> Transaction:
    left = total - index
    return tx(d, f"ORNEK MOBILYA {index}/{total}", each, "installment",
              InstallmentInfo(remaining_count=left, remaining_amount_each=each if left else 0, is_last=left == 0,
                              plan_id=plan, index=index, total_count=total))


def statement(end: str, *txs: Transaction, bank: str = "garanti", last4: str = "1111", total: int = 0,
              due: str | None = None, product: str = "Bonus", **kw) -> Statement:
    e = date.fromisoformat(end)
    cid = card_id(bank, last4)
    return Statement(id=f"st_{bank}{last4}_{end}", bank=bank, product=product, card_last4=last4,
                     period_start=e.replace(day=1), period_end=e, due_date=date.fromisoformat(due) if due else e,
                     statement_total=total, minimum_payment=total // 5, transactions=list(txs), card_id=cid,
                     card_label=label(bank, product, last4), **kw)


# -- cards ----------------------------------------------------------------------------------------


def test_card_id_is_stable_and_label_is_plain():
    assert card_id("garanti", "1111") == card_id("garanti", "1111") != card_id("garanti", "2222")
    assert card_id("garanti", "1111").startswith("cd_") and len(card_id("garanti", "1111")) == 13
    assert label("garanti", "Bonus", "1111") == "Garanti Bonus ··1111"
    assert label("vakifbank", "Worldcard", "0000") == "VakıfBank Worldcard ··0000"
    assert label("yapikredi", "World", None) == "Yapı Kredi World"
    assert label("garanti", "Garanti Bonus", "1111") == "Garanti Bonus ··1111"  # product repeats the bank
    assert label("garanti", "Bonus", "1111", "Market kartım") == "Market kartım ··1111"
    assert label("ornekbank", "", "9999") == "Ornekbank ··9999"


def test_list_cards_uses_the_newest_statement():
    sts = [statement("2099-01-20", product="Bonus"), statement("2099-02-20", product="Bonus Platinum"),
           statement("2099-02-05", bank="yapikredi", last4="2222", product="World")]
    cards = list_cards(sts, {card_id("yapikredi", "2222"): "Gezi"})
    assert [c["label"] for c in cards] == ["Garanti Bonus Platinum ··1111", "Gezi ··2222"]
    g = cards[0]
    assert (g["statement_count"], g["first_month"], g["last_month"], g["latest_statement_id"]) == (
        2, "2099-01", "2099-02", "st_garanti1111_2099-02-20")
    assert g["bank_name"] == "Garanti BBVA" and g["name"] is None


# -- closing plans --------------------------------------------------------------------------------


def three_part_plan() -> list[Statement]:
    return [statement("2099-01-20", plan_row("2099-01-02", "pl_a", 1, 3, 50000)),
            statement("2099-02-20", plan_row("2099-01-02", "pl_a", 2, 3, 50000))]


def test_open_plan_fields_and_summary():
    sched = build_schedule(three_part_plan(), months=3)
    (plan,) = sched.plans
    assert (plan.total_count, plan.paid_count, plan.remaining_count, plan.next_month, plan.next_amount) == (
        3, 2, 1, "2099-03", 50000)
    assert plan.closed is None and plan.card_id == card_id("garanti", "1111")
    assert sched.summary.model_dump() == {"total_remaining": 50000, "plan_count": 1, "last_month": "2099-03"}
    assert sched.months[0].items[0].card_id == plan.card_id


def test_user_closed_plan_leaves_every_total():
    sched = build_schedule(three_part_plan(), months=3, closed_by_user={"pl_a": date(2099, 2, 25)})
    assert sched.plans == [] and sched.months[0].total == 0 and sched.summary.plan_count == 0
    (closed,) = sched.closed_plans
    assert (closed.closed, closed.closed_at, closed.statement_id) == ("user", date(2099, 2, 25), None)
    assert (closed.remaining_count, closed.total_remaining, closed.paid_count, closed.next_month) == (
        1, 50000, 2, None)
    body = sched.api_body()
    assert [p["closed"] for p in body["plans"]] == ["user"]
    assert body["summary"]["total_remaining"] == 0


def test_statement_closes_a_plan_whose_row_is_missing():
    sts = [*three_part_plan(), statement("2099-03-20", tx("2099-03-03", "ORNEK KAFE", 1000))]
    sched = build_schedule(sts, months=3)
    (closed,) = sched.closed_plans
    assert closed.closed == "statement" and closed.statement_id == "st_garanti1111_2099-03-20"
    assert closed.ends == "2099-02" and sched.plans == []


def test_other_cards_and_later_months_do_not_close_a_plan():
    other_card = [*three_part_plan(), statement("2099-03-20", bank="yapikredi", last4="2222")]
    assert build_schedule(other_card, months=3).closed_plans == []
    four = [statement("2099-01-20", plan_row("2099-01-02", "pl_b", 1, 2, 50000)),
            statement("2099-03-20", tx("2099-03-03", "ORNEK KAFE", 1000))]  # the plan ended in February anyway
    sched = build_schedule(four, months=3)
    assert sched.closed_plans == []


def test_plan_still_running_on_the_newer_statement_stays_open():
    sts = [*three_part_plan(), statement("2099-03-20", plan_row("2099-01-02", "pl_a", 3, 3, 50000))]
    sched = build_schedule(sts, months=3)
    assert sched.closed_plans == [] and sched.plans == []  # finished normally: nothing left, nothing closed


# -- per-card forecast ----------------------------------------------------------------------------


def two_cards() -> list[Statement]:
    a = [statement(f"2099-0{m}-20", tx(f"2099-0{m}-05", "ORNEK DEPOLAMA", 2999, category="subscriptions"),
                   tx(f"2099-0{m}-07", "ORNEK MARKET", 10000 * m, category="groceries"),
                   plan_row(f"2099-0{m}-01", "pl_a", m, 6, 50000), total=100000, due=f"2099-0{m + 1}-01")
         for m in (1, 2, 3)]
    b = [statement("2099-03-10", tx("2099-03-02", "ORNEK KAFE", 7000), bank="yapikredi", last4="2222",
                   product="World", total=7000, due="2099-03-20", next_period_end=date(2099, 4, 12),
                   next_due_date=date(2099, 4, 22))]
    return a + b


def test_card_rows_statement_then_forecast():
    sts = two_cards()
    rows = card_rows(sts, regular_payments(sts, []), "TRY", today=date(2099, 3, 25), locale="tr")
    by_card = {r["card_label"]: r for r in rows}
    a, b = by_card["Garanti Bonus ··1111"], by_card["Yapı Kredi World ··2222"]
    # card A: the March statement is due on 1 April, still ahead
    assert (a["estimated"], a["amount"], a["due_date"], a["statement_id"], a["parts"]) == (
        False, 100000, "2099-04-01", "st_garanti1111_2099-03-20", None)
    assert a["days_left"] == 7 and a["minimum_payment"] == 20000
    # card B: its due date passed, so the row is the forecast of its next statement (printed dates)
    nxt = b["next_statement"]
    assert b["estimated"] is True and b["amount"] == nxt["total"] and b["due_date"] == "2099-04-22"
    assert nxt["dates"] == "printed" and nxt["cut_off"] == "2099-04-12" and nxt["month"] == "2099-04"
    assert nxt["note_key"] == "forecast.note.one_month" and b["parts"] == nxt["parts"]
    assert [r["card_label"] for r in rows] == ["Garanti Bonus ··1111", "Yapı Kredi World ··2222"]  # by due date


def test_card_forecast_parts():
    sts = two_cards()
    rows = card_rows(sts, regular_payments(sts, []), "TRY", today=date(2099, 4, 5), locale="en")
    a = next(r for r in rows if r["card_id"] == card_id("garanti", "1111"))
    nxt = a["next_statement"]
    assert a["estimated"] is True and nxt["dates"] == "estimated" and nxt["cut_off"] == "2099-04-20"
    assert nxt["due_date"] == "2099-05-02"  # same gap as the last statement: 12 days
    assert nxt["parts"]["installments"] == 50000 and nxt["parts"]["subscriptions"] == 2999
    assert nxt["parts"]["other"] == 20000  # middle of 10000, 20000, 30000
    assert nxt["total"] == 72999 and nxt["subscriptions"][0]["merchant"] == "ORNEK DEPOLAMA"
    assert nxt["note"].startswith("Installments and regular payments are known") and "3 months" in nxt["note"]


def test_closed_plan_leaves_the_card_forecast():
    sts = two_cards()
    rows = card_rows(sts, regular_payments(sts, []), "TRY", closed_by_user={"pl_a": date(2099, 3, 25)},
                     today=date(2099, 4, 5))
    a = next(r for r in rows if r["card_id"] == card_id("garanti", "1111"))
    assert a["next_statement"]["parts"]["installments"] == 0


def test_forecast_body_adds_rows_and_total():
    sts = two_cards()
    body = next_statement_forecast(sts, [], "TRY", today=date(2099, 3, 25))
    assert {"open_period", "forecast", "backtest", "cards", "total", "currency", "fixed"} <= set(body)
    assert body["total"] == sum(r["amount"] for r in body["cards"])


def test_one_statement_note():
    st = statement("2099-01-20", tx("2099-01-05", "ORNEK KAFE", 4000), total=4000)
    (row,) = card_rows([st], [], "TRY", today=date(2099, 2, 1), locale="tr")
    nxt = row["next_statement"]
    assert (nxt["note_key"], nxt["note_params"], nxt["parts"]["other"]) == ("forecast.note.one_month", {"months": 1},
                                                                            4000)
    assert nxt["note"] == ("Taksitler ve düzenli ödemeler belli. Diğer harcamalar için bu kartın son ekstresindeki "
                           "tutarı aldım.")


# -- missing statements ---------------------------------------------------------------------------


def test_missing_statements_are_one_alert_per_gap():
    sts = [statement("2099-01-20"), statement("2099-02-20"), statement("2099-06-20"), statement("2099-08-20"),
           statement("2099-11-20", bank="yapikredi", last4="2222", product="World"),
           statement("2100-02-20", bank="yapikredi", last4="2222", product="World")]
    alerts = [a for a in compute_alerts(sts, build_schedule(sts, months=1), [], today=date(2100, 2, 21))
              if a.kind == "missing_statement"]
    by_from = {a.params["from"]: a for a in alerts}
    assert sorted(by_from) == ["2099-03", "2099-07", "2099-09", "2099-12"]
    spring = by_from["2099-03"]
    assert spring.params | {} == {"bank": "garanti", "card_last4": "1111", "card": "Garanti Bonus ··1111",
                                  "from": "2099-03", "to": "2099-05", "months": 3,
                                  "month_list": ["2099-03", "2099-04", "2099-05"], "trailing": False}
    assert spring.month == "2099-03" and spring.card_id == card_id("garanti", "1111")
    assert spring.text == ("Garanti Bonus ··1111 için Mart–Mayıs 2099 ekstreleri yok. Bankanın uygulamasından "
                           "PDF olarak indirip ekle.")
    assert by_from["2099-07"].text.startswith("Garanti Bonus ··1111 için Temmuz 2099 ekstresi yok.")
    assert "Aralık 2099–Ocak 2100 ekstreleri yok" in by_from["2099-12"].text
    assert spring.refs.statement_ids == ["st_garanti1111_2099-02-20", "st_garanti1111_2099-06-20"]
    tail = by_from["2099-09"]  # after the latest Garanti statement (August), up to January's cut-off + 7 days
    assert tail.params["trailing"] and tail.params["to"] == "2100-01" and tail.params["months"] == 5
    assert tail.refs.statement_ids == ["st_garanti1111_2099-08-20"]


def _missing(sts, today):
    return [a for a in compute_alerts(sts, build_schedule(sts, months=1), [], today=today)
            if a.kind in ("missing_statement", "statement_overdue")]


def test_trailing_missing_months_up_to_today():
    sts = [statement("2099-08-26"), statement("2099-09-26")]
    # Latest September; late November: October is missing, November is not cut long enough ago.
    (alert,) = _missing(sts, date(2099, 11, 28))
    assert alert.kind == "missing_statement" and alert.params["month_list"] == ["2099-10"]
    assert alert.params["trailing"] and alert.month == "2099-10"
    assert alert.text.startswith("Garanti Bonus ··1111 için Ekim 2099 ekstresi yok.")
    (later,) = _missing(sts, date(2099, 12, 4))  # November 26 + 7 days has passed
    assert later.params["month_list"] == ["2099-10", "2099-11"]
    assert _missing(sts, date(2099, 10, 3)) == []  # October's statement is not due yet


def test_trailing_missing_keeps_the_cut_off_day():
    sts = [statement("2099-01-31")]  # February is clipped to the 28th; March is the 31st again
    assert [a.params["month_list"] for a in _missing(sts, date(2099, 4, 7))] == [["2099-02"]]
    assert [a.params["month_list"] for a in _missing(sts, date(2099, 4, 8))] == [["2099-02", "2099-03"]]


def test_missing_statement_in_english():
    sts = [statement("2099-01-20"), statement("2099-03-20")]
    (gap,) = [a for a in compute_alerts(sts, build_schedule(sts, months=1), [], today=date(2099, 3, 21),
                                        locale="en") if a.kind == "missing_statement"]
    assert gap.text == ("The February 2099 statement of Garanti Bonus ··1111 is missing. Download it as a PDF "
                        "from your bank's app and add it.")


def test_texts_helpers():
    assert month_range("2099-03", "2099-08", "tr") == "Mart–Ağustos 2099"
    assert month_range("2099-11", "2100-02", "en") == "November 2099–February 2100"
    assert money(123456, "TRY", "tr") == "1.234,56 TL" and money(100000, "TRY", "en") == "1,000 TL"
