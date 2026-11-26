"""Rule-based saving suggestions. Invented merchants and amounts."""

from __future__ import annotations

from datetime import date

from kalan.cards import card_id, label
from kalan.insights.card_fee import find_card_fees
from kalan.models import InstallmentInfo, Statement, Transaction
from kalan.regular import frequent_places, regular_payments
from kalan.schedule import build_schedule
from kalan.suggestions import ai_items, rule_suggestions

_n = iter(range(1, 100_000))


def tx(d: str, desc: str, amount: int, kind: str = "purchase", category: str | None = None,
       direction: str = "debit", **kw) -> Transaction:
    return Transaction(id=f"tx_{next(_n):06d}", date=date.fromisoformat(d), description=desc, amount=amount,
                       direction=direction, kind=kind, category=category, **kw)


def statement(end: str, *txs: Transaction) -> Statement:
    e = date.fromisoformat(end)
    return Statement(id=f"st_{end}", bank="garanti", product="Bonus", card_last4="1111",
                     period_start=e.replace(day=1), period_end=e, due_date=e, statement_total=0, minimum_payment=0,
                     transactions=list(txs), card_id=card_id("garanti", "1111"),
                     card_label=label("garanti", "Bonus", "1111"))


def suggest(sts: list[Statement], locale: str = "tr") -> list[dict]:
    items = regular_payments(sts, [])
    return rule_suggestions(sts, [], items, frequent_places(sts, [], "TRY", items), find_card_fees(sts),
                            build_schedule(sts, months=12), "TRY", locale)


def kinds(found: list[dict]) -> list[str]:
    return [s["kind"] for s in found]


def monthly(m: int, *rows: Transaction) -> Statement:
    return statement(f"2099-{m:02d}-20", *rows)


def test_duplicate_cloud_storage():
    sts = [monthly(m, tx(f"2099-{m:02d}-03", "GOOGLE ONE", 4999, category="subscriptions"),
                   tx(f"2099-{m:02d}-04", "ICLOUD", 3999, category="subscriptions")) for m in (1, 2)]
    (dup,) = [s for s in suggest(sts) if s["kind"] == "duplicate_service"]
    assert dup["monthly_saving"] == 3999 and dup["yearly_saving"] == 3999 * 12
    assert dup["params"]["merchants"] == ["ICLOUD", "GOOGLE ONE"] and dup["params"]["group"] == "cloud_storage"
    assert dup["params"]["monthly_amount"] == 3999 and dup["source"] == "rules"
    assert dup["link"].startswith("/recurring?id=sb_") and dup["message_key"] == "suggestion.duplicate_service"
    assert dup["title"] == "Aynı işi yapan 2 aboneliğin var"
    assert "39,99 TL" in dup["body"] and "bulut depolama" in dup["body"]


def test_different_services_are_no_duplicate():
    sts = [monthly(m, tx(f"2099-{m:02d}-03", "GOOGLE ONE", 4999, category="subscriptions"),
                   tx(f"2099-{m:02d}-04", "NETFLIX", 9999, category="subscriptions")) for m in (1, 2)]
    assert "duplicate_service" not in kinds(suggest(sts))


def test_price_rise():
    sts = [monthly(1, tx("2099-01-03", "ORNEK MUZIK", 4999, category="subscriptions")),
           monthly(2, tx("2099-02-03", "ORNEK MUZIK", 5999, category="subscriptions"))]
    (rise,) = [s for s in suggest(sts, "en") if s["kind"] == "price_rise"]
    assert rise["monthly_saving"] is None and rise["params"]["amount_previous"] == 4999
    assert rise["title"] == "ORNEK MUZIK raised its price" and "49.99 TL" in rise["body"]
    assert rise["card_id"] == card_id("garanti", "1111")


def test_card_fee_and_its_refund():
    fee = tx("2099-01-10", "YILLIK KART UCRETI", 120000, kind="fee")
    (item,) = [s for s in suggest([monthly(1, fee)]) if s["kind"] == "card_fee"]
    assert item["yearly_saving"] == 120000 and item["monthly_saving"] == 10000
    assert item["link"] == f"/rights?id={fee.id}" and item["params"]["card"] == "Garanti Bonus ··1111"
    refunded = [monthly(1, fee), monthly(2, tx("2099-02-10", "YILLIK KART UCRETI IADE", 120000, kind="fee",
                                               direction="credit"))]
    assert "card_fee" not in kinds(suggest(refunded))


def test_interest_and_late_fee_on_the_latest_statement():
    st = monthly(1, tx("2099-01-20", "AKDI FAIZ", 5000, kind="interest"),
                 tx("2099-01-20", "GECIKME UCRETI", 2000, kind="fee"),
                 tx("2099-01-20", "BSMV", 750, kind="tax"), tx("2099-01-05", "ORNEK KITAP", 3000))
    (item,) = [s for s in suggest([st]) if s["kind"] == "interest"]
    assert item["monthly_saving"] == 7750 and item["params"]["month"] == "2099-01"
    assert item["link"] == "/statements?id=st_2099-01-20"
    assert "interest" not in kinds(suggest([monthly(1, tx("2099-01-05", "ORNEK KITAP", 3000))]))


def test_foreign_currency_average_over_three_months():
    sts = [monthly(m, tx(f"2099-{m:02d}-03", "ORNEK STORE", 30000, original_amount=1000, original_currency="USD"))
           for m in (1, 2, 3, 4)]
    (item,) = [s for s in suggest(sts) if s["kind"] == "foreign_currency"]
    assert item["params"] == {"monthly_average": 30000, "currencies": ["USD"], "months": 3}
    assert item["monthly_saving"] is None


def test_frequent_dining_place_halved():
    rows = []
    for m in (1, 2, 3):
        rows.append(monthly(m, tx(f"2099-{m:02d}-02", "ORNEK YEMEK", 40000, category="dining"),
                            tx(f"2099-{m:02d}-12", "ORNEK YEMEK", 20000, category="dining"),
                            tx(f"2099-{m:02d}-15", "ORNEK MARKET", 200000, category="groceries"),
                            tx(f"2099-{m:02d}-16", "ORNEK MARKET", 200000, category="groceries")))
    found = suggest(rows)
    (item,) = [s for s in found if s["kind"] == "frequent_place"]  # the market is not discretionary
    assert item["params"]["monthly_average"] == 60000 and item["monthly_saving"] == 30000
    assert item["yearly_saving"] == 360000 and item["link"] == "/spending?merchant=ORNEK%20YEMEK"
    assert item["title"] == "ORNEK YEMEK için ayda ortalama 600 TL harcıyorsun"
    assert item["body"] == "Bu yarıya inse yılda 3.600 TL kalır."


def test_small_frequent_place_is_no_suggestion():
    rows = [monthly(m, tx(f"2099-{m:02d}-02", "ORNEK KAFE", 1000, category="dining"),
                    tx(f"2099-{m:02d}-12", "ORNEK KAFE", 1000, category="dining"),
                    tx(f"2099-{m:02d}-15", "ORNEK MARKET", 500000, category="groceries")) for m in (1, 2)]
    assert "frequent_place" not in kinds(suggest(rows))


def test_installment_relief():
    def row(plan: str, index: int, total: int, each: int) -> Transaction:
        left = total - index
        return tx("2099-01-02", f"ORNEK {plan} {index}/{total}", each, kind="installment",
                  installment=InstallmentInfo(remaining_count=left, remaining_amount_each=each, is_last=left == 0,
                                              plan_id=plan, index=index, total_count=total))
    st = monthly(1, row("pl_short", 1, 3, 40000), row("pl_long", 1, 10, 10000))
    (item,) = [s for s in suggest([st]) if s["kind"] == "installment_relief"]
    assert item["params"] == {"month": "2099-04", "amount": 40000, "plans": 1}
    assert item["title"] == "Nisan 2099 taksitlerin 400 TL azalıyor"


def test_order_ids_and_ai_items():
    fee = tx("2099-01-10", "YILLIK KART UCRETI", 120000, kind="fee")
    st = monthly(1, fee, tx("2099-01-20", "AKDI FAIZ", 50000, kind="interest"))
    found = suggest([st])
    assert kinds(found)[:2] == ["interest", "card_fee"]  # biggest monthly saving first
    assert [s["id"] for s in found] == [s["id"] for s in suggest([st])]  # stable
    ai = ai_items({"provider": "fake", "created_at": "2099-01-01T00:00:00Z", "currency": "TRY",
                   "items": [{"title": "Başlık", "body": "Metin.", "link": "/spending"}]})
    assert ai[0]["source"] == "ai" and ai[0]["monthly_saving"] is None and ai[0]["kind"] == "ai"
    assert ai_items(None) == []
