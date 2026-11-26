"""HTTP tests of contract v0.4.0 (task 6). Fake provider only; invented manual rows."""

from __future__ import annotations

import json

from kalan.ai.adapters import AdapterError
from kalan.api.app import create_app
from kalan.store import Store
from tests.conftest import VB_PDFS, upload

LABEL = "VakıfBank Worldcard ··0000"


def payload_of(prompt: str) -> dict:
    return json.loads(prompt.split("Statement data:\n", 1)[1].split("\n", 1)[0])


def model(prompt: str) -> str:
    """Fake provider: categories for a statement, suggestions for the aggregate prompt."""
    if "saving suggestions" in prompt:
        return json.dumps({"suggestions": [{"title": "Yemek siparişini azalt", "body": "Ayda iki kez yeter.",
                                            "screen": "spending"}]})
    ids = [t["id"] for t in payload_of(prompt)["transactions"] if "category" not in t]
    return json.dumps({"categories": {i: "other" for i in ids},
                       "commentary": {"summary": "Kısa özet.", "watch_items": []}, "recurring": []})


def manual(client, d: str, desc: str, amount: int) -> None:
    r = client.post("/transactions", json={"date": d, "description": desc, "amount": amount, "currency": "TRY",
                                           "direction": "debit"})
    assert r.status_code == 201, r.text


def select(client) -> None:
    assert client.put("/providers/selected", json={"id": "fake"}).status_code == 200


def wait(client, job_id: str) -> dict:
    client.app.state.runner.wait(job_id, 20)
    return client.get(f"/jobs/{job_id}").json()


# -- cards ----------------------------------------------------------------------------------------


def test_cards_and_names_everywhere(client):
    assert upload(client, VB_PDFS[10]).status_code == 201
    (card,) = client.get("/cards").json()["items"]
    assert card["label"] == LABEL and card["bank_name"] == "VakıfBank" and card["card_last4"] == "0000"
    assert card["name"] is None and card["statement_count"] == 1

    named = client.put(f"/cards/{card['id']}", json={"name": "Ana kart"}).json()
    assert named["name"] == "Ana kart" and named["label"] == "Ana kart ··0000"
    st = client.get("/statements").json()["items"][0]
    assert (st["card_id"], st["card_label"]) == (card["id"], "Ana kart ··0000")
    rows = client.get("/transactions").json()["items"]
    assert rows and all(t["card_id"] == card["id"] and t["card_label"] == "Ana kart ··0000" for t in rows)
    plans = client.get("/installments/schedule").json()["plans"]
    assert plans and all(p["card_label"] == "Ana kart ··0000" for p in plans)

    assert client.put(f"/cards/{card['id']}", json={"name": None}).json()["label"] == LABEL
    assert client.put("/cards/cd_0000000000", json={"name": "x"}).status_code == 404
    assert client.put(f"/cards/{card['id']}", json={"name": "x" * 41}).status_code == 422


def test_loose_rows_have_no_card_and_category_source(client):
    manual(client, "2099-01-05", "ORNEK MARKET", 12345)
    (row,) = client.get("/transactions").json()["items"]
    assert row["card_id"] is None and row["card_label"] is None
    assert row["merchant_unreadable"] is False and "category_source" in row


def test_rule_category_source_on_statement_rows(client):
    assert upload(client, VB_PDFS[10]).status_code == 201
    rows = client.get("/transactions").json()["items"]
    assert any(t["category_source"] == "rule" for t in rows)
    assert all(t["category_source"] is None for t in rows if t["category"] is None)


# -- plans ----------------------------------------------------------------------------------------


def test_close_and_reopen_a_plan(client):
    for p in VB_PDFS[8:10]:
        assert upload(client, p).status_code == 201
    before = client.get("/installments/schedule").json()
    plan = before["plans"][0]
    closed = client.post(f"/plans/{plan['plan_id']}/close")
    assert closed.status_code == 200 and closed.json()["closed"] == "user" and closed.json()["closed_at"]
    assert client.post(f"/plans/{plan['plan_id']}/close").json() == closed.json()  # again: the same

    after = client.get("/installments/schedule").json()
    assert after["plans"][-1]["plan_id"] == plan["plan_id"] and after["plans"][-1]["closed"] == "user"
    open_after = [p for p in after["plans"] if p["closed"] is None]
    assert len(open_after) == len(before["plans"]) - 1
    assert after["summary"]["total_remaining"] == before["summary"]["total_remaining"] - plan["total_remaining"]
    assert all(i["plan_id"] != plan["plan_id"] for m in after["months"] for i in m["items"])
    forecast = client.get("/insights/forecast").json()
    assert all(i["plan_id"] != plan["plan_id"] for i in forecast["forecast"]["installment_items"])
    assert all(i["plan_id"] != plan["plan_id"] for c in forecast["cards"]
               for i in c["next_statement"]["installment_items"])

    assert client.delete(f"/plans/{plan['plan_id']}/close").status_code == 204
    assert client.get("/installments/schedule").json() == before
    assert client.delete(f"/plans/{plan['plan_id']}/close").status_code == 404
    assert client.post("/plans/pl_missing/close").json()["error"]["code"] == "plan_not_found"


# -- regular payments -----------------------------------------------------------------------------


def test_subscription_expected_amount(client):
    for d in ("2099-01-05", "2099-02-05"):
        manual(client, d, "ORNEK KULUP", 50000)
    (item,) = client.get("/subscriptions").json()["items"]
    sid = item["id"]
    put = client.put(f"/subscriptions/{sid}/expected", json={"amount": 30000})
    assert put.status_code == 200 and put.json()["expected_amount"] == 30000 and put.json()["monthly_amount"] == 30000
    body = client.get("/subscriptions").json()
    assert body["totals"] == [{"currency": "TRY", "subscription": 30000, "bill": 0}]
    assert client.delete(f"/subscriptions/{sid}/expected").status_code == 204
    assert client.delete(f"/subscriptions/{sid}/expected").json()["error"]["code"] == "expected_not_set"
    assert client.put("/subscriptions/sb_missing/expected", json={"amount": 1}).status_code == 404
    assert client.put(f"/subscriptions/{sid}/expected", json={"amount": 0}).status_code == 422


def test_expected_amount_clears_itself(client, store):
    for d in ("2099-01-05", "2099-02-05"):
        manual(client, d, "ORNEK KULUP", 50000)
    (item,) = client.get("/subscriptions").json()["items"]
    store.put_value("subscription_expected", {item["id"]: {"amount": 30000, "since": "2099-02-10"}})
    manual(client, "2099-03-05", "ORNEK KULUP", 30000)
    (item,) = client.get("/subscriptions").json()["items"]
    assert item["expected_amount"] is None and item["amount_last"] == 30000
    assert store.get_value("subscription_expected") == {}


def test_frequent_places_route(client):
    for d in ("2099-01-03", "2099-01-20", "2099-02-07"):
        manual(client, d, "ORNEK LOKANTA", 20000)
    body = client.get("/insights/frequent-places", params={"months": 6}).json()
    (place,) = body["items"]
    assert place["merchant"] == "ORNEK LOKANTA" and place["visits"] == 3 and place["monthly_average"] == 30000
    assert client.get("/subscriptions").json()["items"] == []
    assert client.get("/insights/frequent-places", params={"months": 0}).status_code == 422


# -- suggestions ----------------------------------------------------------------------------------


def test_suggestions_without_ai(client):
    for m in ("01", "02"):
        manual(client, f"2099-{m}-03", "GOOGLE ONE", 4999)
        manual(client, f"2099-{m}-04", "ICLOUD", 3999)
    body = client.get("/suggestions", params={"locale": "en"}).json()
    assert body["ai"] == {"status": "none", "provider": None, "created_at": None, "job_id": None}
    (dup,) = [s for s in body["items"] if s["kind"] == "duplicate_service"]
    assert dup["title"] == "2 subscriptions do the same job" and dup["monthly_saving"] == 3999
    assert set(dup) == {"id", "kind", "title", "body", "monthly_saving", "yearly_saving", "currency", "card_id",
                        "card_label", "source", "link", "message_key", "params"}


# -- AI batch -------------------------------------------------------------------------------------


def test_analysis_all_runs_every_statement_then_suggestions(client, fake):
    fake.answers = model
    for p in VB_PDFS[8:10]:
        assert upload(client, p).status_code == 201
    before = {t["id"]: t["category"] for t in client.get("/transactions").json()["items"]}
    assert client.get("/analysis/all").json() is None
    select(client)
    started = client.post("/analysis/all")
    assert started.status_code == 202 and started.json()["kind"] == "analysis_all" and started.json()["total"] == 2
    job = wait(client, started.json()["job_id"])
    assert job | {} == {"job_id": started.json()["job_id"], "kind": "analysis_all", "statement_id": None,
                        "status": "done", "phase": "done", "done": 2, "total": 2, "failed": 0, "error": None}
    assert client.get("/analysis/all").json() == job
    assert all(s["analysis_status"] == "done" for s in client.get("/statements").json()["items"])
    rows = client.get("/transactions").json()["items"]
    for t in rows:  # rules first: the model only filled what had no category
        if before[t["id"]] is not None:
            assert t["category"] == before[t["id"]] and t["category_source"] == "rule"
        else:
            assert t["category"] == "other" and t["category_source"] == "model"
    advice = next(p for p in fake.prompts if "saving suggestions" in p)
    assert "tx_" not in advice and "transactions" not in advice.split("Totals:")[1]
    body = client.get("/suggestions").json()
    ai = [s for s in body["items"] if s["source"] == "ai"]
    assert ai and ai[0]["monthly_saving"] is None and ai[0]["link"] == "/spending"
    assert body["ai"]["status"] == "done" and body["ai"]["provider"] == "fake"


def test_analysis_all_goes_on_after_a_failure(client, fake):
    calls = {"n": 0}

    def flaky(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise AdapterError("time out")
        if "saving suggestions" in prompt:
            raise AdapterError("time out")
        return model(prompt)

    fake.answers = flaky
    for p in VB_PDFS[8:10]:
        assert upload(client, p).status_code == 201
    select(client)
    job = wait(client, client.post("/analysis/all").json()["job_id"])
    assert job["status"] == "done" and job["done"] == 2 and job["failed"] == 1 and job["error"]
    statuses = sorted(s["analysis_status"] for s in client.get("/statements").json()["items"])
    assert statuses == ["done", "failed"]
    assert client.get("/suggestions").json()["ai"]["status"] == "failed"
    assert [s for s in client.get("/suggestions").json()["items"] if s["source"] == "ai"] == []


def test_analysis_all_takes_done_statements_with_uncategorised_rows(client, fake, store):
    fake.answers = model
    assert upload(client, VB_PDFS[9]).status_code == 201
    st_id = client.get("/statements").json()["items"][0]["id"]
    store.set_analysis_status(st_id, "done")
    select(client)
    job = wait(client, client.post("/analysis/all").json()["job_id"])
    assert job["total"] == 1 and job["done"] == 1


def test_upload_with_provider_is_analysed(client, fake):
    fake.answers = model
    select(client)
    resp = upload(client, VB_PDFS[9])
    assert resp.status_code == 201
    st = resp.json()
    assert st["analysis_job_id"] and st["analysis_status"] == "pending"
    job = wait(client, st["analysis_job_id"])
    assert job["status"] == "done" and job["done"] == 1
    assert client.get(f"/statements/{st['id']}").json()["analysis_status"] == "done"


def test_analysis_all_needs_a_provider(client):
    assert client.post("/analysis/all").json()["error"]["code"] == "no_provider"


def test_single_statement_job_shape(client, fake):
    fake.answers = model
    assert upload(client, VB_PDFS[9]).status_code == 201
    st_id = client.get("/statements").json()["items"][0]["id"]
    select(client)  # selected after the upload: no automatic batch
    job_id = client.post(f"/statements/{st_id}/analyze").json()["job_id"]
    job = wait(client, job_id)
    assert job | {} == {"job_id": job_id, "kind": "statement", "statement_id": st_id, "status": "done",
                        "phase": "done", "done": 1, "total": 1, "failed": 0, "error": None}


# -- start-up pass --------------------------------------------------------------------------------


def test_start_fills_categories_rules_now_cover(config, store, registry):
    from fastapi.testclient import TestClient

    with TestClient(create_app(config=config, store=store, registry=registry)) as c:
        manual(c, "2099-01-05", "ORNEK ECZANESI", 1000)
    with store._tx() as con:  # as if an older version had stored it without a category
        con.execute("UPDATE transactions SET category = NULL, category_source = NULL")
    with TestClient(create_app(config=config, store=Store(config.db_path), registry=registry)) as c:
        (row,) = c.get("/transactions").json()["items"]
        assert row["category"] == "health" and row["category_source"] == "rule"

