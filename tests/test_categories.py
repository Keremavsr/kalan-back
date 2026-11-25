"""Merchant names and rule-based categories (built-in YAML, user rules in SQLite, AI only for the rest)."""

from __future__ import annotations

from datetime import date

import pytest

from kalan import categories
from kalan.ai.prompt import CATEGORIES
from kalan.models import Transaction
from kalan.text import merchant_key, normalize_merchant
from tests.conftest import VB_PDFS, upload
from tests.test_api import answer_for, run_job


def tx(description: str, kind: str = "purchase", merchant: str | None = None) -> Transaction:
    return Transaction(id="tx_1", date=date(2099, 1, 15), description=description, amount=123456,
                       direction="debit", kind=kind, merchant=merchant)


@pytest.mark.parametrize(("printed", "name"), [
    ("IYZICO*ORNEK KAFE ISTANBUL TR", "ORNEK KAFE"),
    ("PAYTR *ORNEK MARKET 000123", "ORNEK MARKET"),
    ("PAYPAL *ORNEKAPP 4029357733 LU", "ORNEKAPP"),
    ("A101 MAGAZACILIK 1234 ANKARA", "A101 MAGAZACILIK"),
    ("ORNEKMARKET-0001-MERKEZ", "ORNEKMARKET-MERKEZ"),
    ("DENEME KAFE          ISTANBUL       TR", "DENEME KAFE"),
    ("ORNEK MAGAZA KADIKOY/ISTANBUL", "ORNEK MAGAZA"),
    ("ORNEK SPOR 3/6", "ORNEK SPOR"),
    ("ORNEKPAY *ORNEKCARSI", "ORNEKPAY ORNEKCARSI"),  # unknown processor: only the star goes
    ("ISTANBUL", ""),  # only a city: no merchant name (merchant_unreadable)
    ("ISTANBUL KADIKOY ORNEK CD", ""),  # only an address
    ("ORNEK KAFE ORNEK CD NO:5 KADIKOY", "ORNEK KAFE"),  # address after the name goes
    ("ANKARA CANKAYA ORNEK BLV ORNEK FIRIN", "ORNEK FIRIN"),  # address before the name goes
    ("MONEYPAY ORNEK MARKET", "ORNEK MARKET"),  # unambiguous processor, no star
    ("MONEYPAY*ORNEK MARKET ISTANBUL", "ORNEK MARKET"),
    ("MOKA UNITED*ORNEK PASTANE", "ORNEK PASTANE"),
    ("PARAM KAFE", "PARAM KAFE"),  # short processor names need the star
    ("PARAM*ORNEK KAFE", "ORNEK KAFE"),
    ("ORNEK STORE 9.99 USD", "ORNEK STORE"),
])
def test_merchant_names(printed, name):
    assert normalize_merchant(printed) == name


def test_plan_key_is_unchanged():
    """Installment plan ids of the AI fallback and imports are built on merchant_key: it must not move."""
    assert merchant_key("IYZICO*ORNEK KAFE 2/6 ISTANBUL TR") == "IYZICO*ORNEK KAFE ISTANBUL TR"


def test_builtin_file_is_valid():
    b = categories.builtin()
    assert b.rules and all(r.category in CATEGORIES for r in b.rules)
    assert set(b.kinds.values()) <= set(CATEGORIES)
    assert "istanbul" in b.tails


@pytest.mark.parametrize(("description", "kind", "category"), [
    ("MIGROS-ORNEK", "purchase", "groceries"),
    ("ŞOK MARKET 1234", "purchase", "groceries"),
    ("GETIR", "purchase", "groceries"),
    ("GETIR YEMEK", "purchase", "dining"),  # longest phrase wins
    ("ORNEK ELEKTRONIK AVM", "installment", "electronics"),
    ("IYZICO*NETFLIX.COM", "purchase", "subscriptions"),
    ("TEST ECZANESI", "purchase", "health"),
    ("YILLIK KART ÜCRETİ", "fee", "fees"),
    ("AKDİ FAİZ", "interest", "fees"),
    ("Vergi", "tax", "taxes"),
    ("ÖDEMENİZ İÇİN TEŞEKKÜRLER", "payment", "transfers"),
])
def test_builtin_categories(description, kind, category):
    t = tx(description, kind, normalize_merchant(description))
    assert categories.categorize(t) == (category, "kind" if kind != "purchase" and kind != "installment" else "builtin")


def test_uncovered_stays_for_the_model():
    assert categories.categorize(tx("ORNEKPAY ORNEKCARSI")) is None
    assert categories.categorize(tx("MIGROSLAND")) is None  # whole words only


def test_user_rules_come_first():
    rules = [categories.Rule.of("ornekcarsi", "home"), categories.Rule.of("migros", "shopping")]
    assert categories.categorize(tx("ORNEKPAY ORNEKCARSI"), rules) == ("home", "user_rule")
    assert categories.categorize(tx("MIGROS-ORNEK"), rules) == ("shopping", "user_rule")
    with pytest.raises(ValueError):
        categories.Rule.of("x", "not-a-category")
    with pytest.raises(ValueError):
        categories.Rule.of(" * ", "home")


def test_apply_keeps_existing_categories():
    keep = tx("MIGROS-ORNEK").model_copy(update={"category": "home"})
    out = categories.apply([keep, tx("MIGROS-ORNEK"), tx("ORNEKPAY ORNEKCARSI")])
    assert [t.category for t in out] == ["home", "groceries", None]


def test_upload_applies_rules(client):
    st = upload(client, VB_PDFS[2]).json()
    by_merchant = {t["merchant"]: t["category"] for t in st["transactions"] if t["merchant"]}
    assert by_merchant.get("MIGROS-ORNEK") == "groceries"
    stored = client.get(f"/statements/{st['id']}").json()
    assert stored["transactions"] == st["transactions"]


def test_rule_routes(client):
    assert client.get("/category-rules").json() == {"items": [], "categories": list(CATEGORIES)}
    st = upload(client, VB_PDFS[2]).json()
    target = next(t for t in st["transactions"] if t["merchant"] == "MIGROS-ORNEK")
    r = client.post("/category-rules", json={"pattern": "Migros-Ornek", "category": "home"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["rule"]["pattern"] == "Migros-Ornek" and body["rule"]["category"] == "home"
    assert body["applied"] >= 1
    after = {t["id"]: t["category"] for t in client.get(f"/statements/{st['id']}").json()["transactions"]}
    assert after[target["id"]] == "home"
    dup = client.post("/category-rules", json={"pattern": "MİGROS-ORNEK", "category": "dining"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_rule"
    bad = client.post("/category-rules", json={"pattern": "x", "category": "nope"})
    assert bad.status_code == 422
    empty = client.post("/category-rules", json={"pattern": "***", "category": "home"})
    assert empty.json()["error"]["code"] == "invalid_pattern"
    rule_id = body["rule"]["id"]
    assert [i["id"] for i in client.get("/category-rules").json()["items"]] == [rule_id]
    assert client.delete(f"/category-rules/{rule_id}").status_code == 204
    assert client.delete(f"/category-rules/{rule_id}").status_code == 404
    # a deleted rule's categories stay; the next upload uses the built-in rules again
    assert {t["id"]: t["category"] for t in client.get(f"/statements/{st['id']}").json()["transactions"]} == after


def test_rule_never_overrides_a_hand_set_category(client, store):
    hand = tx("MIGROS-ORNEK", merchant="MIGROS-ORNEK").model_copy(
        update={"id": "tx_hand", "source": "manual", "category": "health"})
    store.add_manual(hand)
    assert client.post("/category-rules", json={"pattern": "migros-ornek", "category": "home"}).status_code == 201
    assert store.get_transaction("tx_hand").category == "health"


def test_rule_applies_to_new_uploads(client):
    client.post("/category-rules", json={"pattern": "streamflix", "category": "entertainment", "apply": False})
    st = upload(client, VB_PDFS[11]).json()
    cats = {t["merchant"]: t["category"] for t in st["transactions"] if t["merchant"]}
    assert cats["STREAMFLIX"] == "entertainment"


def test_model_only_categorises_what_rules_left(client, fake):
    st = upload(client, VB_PDFS[11]).json()
    covered = {t["id"]: t["category"] for t in st["transactions"] if t["category"] is not None}
    left = {t["id"] for t in st["transactions"] if t["category"] is None}
    assert covered and left
    client.put("/providers/selected", json={"id": "fake"})
    fake.answers = lambda p: answer_for(p, "other")
    assert run_job(client, st["id"])["status"] == "done"
    prompt = fake.prompts[0]
    assert "already have a category keep it" in prompt
    after = {t["id"]: t["category"] for t in client.get(f"/statements/{st['id']}").json()["transactions"]}
    assert all(after[i] == c for i, c in covered.items())
    assert all(after[i] == "other" for i in left)
