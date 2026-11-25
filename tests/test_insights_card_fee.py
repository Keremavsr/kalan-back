"""Annual card fee detection and the refund letter prompt (invented statements)."""

from __future__ import annotations

from datetime import date

from kalan.insights.card_fee import find_card_fees, refund_letter_prompt, route_for
from tests.insights_data import statement, tx


def fee_statements():
    a = statement("st_a", period_end=date(2099, 1, 15), transactions=[
        tx("t1", date(2099, 1, 3), 99999, kind="fee", description="YILLIK KART UCRETI"),
        tx("t2", date(2099, 1, 3), 14999, kind="fee", description="YILLIK KART UCRETI BSMV"),
        tx("t3", date(2099, 1, 5), 123456, description="ORNEK MARKET"),
        tx("t4", date(2099, 1, 6), 49999, kind="fee", description="EK KART AIDATI"),
    ])
    b = statement("st_b", period_end=date(2099, 2, 15), transactions=[
        tx("t5", date(2099, 2, 1), 49999, kind="fee", description="EK KART AIDATI IADESI", direction="credit"),
    ])
    return [a, b]


def test_find_card_fees():
    fees = find_card_fees(fee_statements())
    assert [f["transaction_id"] for f in fees] == ["t4", "t1"]
    by = {f["transaction_id"]: f for f in fees}
    assert by["t4"]["supplementary_card"] and by["t4"]["refunded"]
    assert not by["t1"]["supplementary_card"] and not by["t1"]["refunded"]
    assert by["t1"]["amount"] == 99999 and by["t1"]["card_last4"] == "0000"


def test_prompt_keeps_placeholders_and_cites_law():
    fee = find_card_fees(fee_statements())[1]
    built = refund_letter_prompt(fee)
    assert built["route"] == "hakem_heyeti"
    assert "[AD SOYAD]" in built["prompt"] and "[TC KİMLİK NO]" in built["prompt"]
    assert "999,99 TRY" in built["prompt"] and "6502" in built["prompt"]
    assert "186.000,00 TL" in built["prompt"]
    assert built["hakem_heyeti"]["limit_below"] == 18600000
    assert route_for(18600000, "TRY") == "consumer_court" and route_for(18599999, "TRY") == "hakem_heyeti"
    assert route_for(100, "USD") == "consumer_court"


def test_routes(client, store, fake):
    for st in fee_statements():
        store.add_statement(st)
    items = client.get("/insights/card-fees").json()["items"]
    assert len(items) == 2
    prompt = client.get("/insights/card-fees/t1/prompt").json()
    assert prompt["fee"]["transaction_id"] == "t1"
    assert client.get("/insights/card-fees/t3/prompt").status_code == 404
    assert client.post("/insights/card-fees/t1/draft").json()["error"]["code"] == "no_provider"
    client.put("/providers/selected", json={"id": "fake"})
    fake.answers.append("1. Banka başvurusu\n[AD SOYAD] [TC KİMLİK NO]\n2. Hakem Heyeti başvurusu\n[ADRES]")
    draft = client.post("/insights/card-fees/t1/draft").json()
    assert draft["provider"] == "fake" and draft["placeholders_kept"] == ["[AD SOYAD]", "[TC KİMLİK NO]", "[ADRES]"]
    assert "[TELEFON]" in draft["placeholders_missing"]
    assert fake.prompts and "YILLIK KART UCRETI" in fake.prompts[-1]
