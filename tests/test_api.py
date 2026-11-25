"""HTTP API against docs/api.md v0.1.1, with a temporary database and the fake model adapter."""

from __future__ import annotations

import json
from datetime import date

import pymupdf
import pytest

from kalan.ai.adapters import ProviderRegistry, default_adapters
from kalan.api import create_app
from kalan.pipeline import parse_pdf
from kalan.schedule import build_schedule
from tests.conftest import VB_PDFS, upload

SUMMARY_KEYS = {"id", "bank", "product", "card_last4", "period_start", "period_end", "due_date", "currency",
                "statement_total", "minimum_payment", "reconciliation", "analysis_status",
                "needs_review", "review"}  # v0.2.0 + review (v0.3.0)
TX_KEYS = {"id", "date", "description", "amount", "currency", "direction", "kind", "installment", "category",
           "merchant", "source"}


def assert_error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    return body["error"]


def payload_of(prompt: str) -> dict:
    return json.loads(prompt.split("Statement data:\n", 1)[1].split("\n", 1)[0])


def answer_for(prompt: str, category: str = "shopping", recurring: bool = False) -> str:
    data = payload_of(prompt)
    ids = [t["id"] for t in data["transactions"]]
    return json.dumps({
        "categories": {i: category for i in ids},
        "commentary": {"summary": "Kısa bir özet.", "watch_items": [
            {"title": "Faiz", "detail": "Bu ay faiz var.", "transaction_ids": [*ids[:1], "tx_unknown"]}]},
        "recurring": ids[:1] if recurring else [],
    })


def blank_pdf(text: str = "Hello, this is not a bank statement.") -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    return doc.tobytes()


# -- health, providers, settings ------------------------------------------------------------------


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0"}


def test_responses_are_never_cached(client):
    # statement data must not end up in the Electron or browser disk cache
    for path in ("/health", "/statements", "/no-such-route"):
        assert client.get(path).headers["cache-control"] == "no-store"


def test_providers_contract_order_without_real_tools(config, store):
    app = create_app(config=config, store=store, registry=ProviderRegistry(default_adapters()))
    from fastapi.testclient import TestClient

    body = TestClient(app).get("/providers").json()
    ids = [p["id"] for p in body["providers"]]
    assert ids == ["claude-cli", "codex-cli", "agy-cli", "gemini-cli", "openai-compatible", "local"]
    assert all(set(p) == {"id", "name", "available", "detail"} for p in body["providers"])
    assert not any(p["available"] for p in body["providers"])
    assert body["selected"] is None
    claude = body["providers"][0]
    assert claude["name"] == "Claude Code" and claude["detail"] == "claude not found on PATH"


def test_provider_selection(client):
    body = client.put("/providers/selected", json={"id": "fake"}).json()
    assert body["selected"] == "fake"
    assert client.get("/providers").json()["selected"] == "fake"
    assert_error(client.put("/providers/selected", json={"id": "nope"}), 422, "unknown_provider")
    err = assert_error(client.put("/providers/selected", json={"id": "fake-off"}), 409, "provider_unavailable")
    assert err["details"]["id"] == "fake-off"
    assert client.get("/providers").json()["selected"] == "fake"
    assert client.put("/providers/selected", json={"id": None}).json()["selected"] is None


def test_settings(client):
    assert client.get("/settings").json() == {"locale": "tr", "base_currency": "TRY", "months_ahead": 12}
    assert client.put("/settings", json={"locale": "en", "months_ahead": 6}).json() == {
        "locale": "en", "base_currency": "TRY", "months_ahead": 6}
    assert client.get("/settings").json()["months_ahead"] == 6
    assert_error(client.put("/settings", json={"locale": "de"}), 422, "validation_error")
    assert_error(client.put("/settings", json={"months_ahead": 0}), 422, "validation_error")
    assert_error(client.put("/settings", json={"colour": "red"}), 422, "validation_error")


# -- statements -----------------------------------------------------------------------------------


def test_upload_and_read_statement(client):
    resp = upload(client, VB_PDFS[11])
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert SUMMARY_KEYS | {"transactions", "interest_rates"} <= set(st)
    assert st["bank"] == "vakifbank" and st["card_last4"] == "0000"
    assert st["reconciliation"]["status"] == "ok"
    assert st["analysis_status"] == "none"
    assert st["transactions"] and all(set(t) >= TX_KEYS for t in st["transactions"])
    assert all(isinstance(v, str) for v in st["interest_rates"].values())
    assert client.get(f"/statements/{st['id']}").json() == st


def test_statement_list_newest_first(client):
    for path in (VB_PDFS[3], VB_PDFS[5], VB_PDFS[4]):
        assert upload(client, path).status_code == 201
    items = client.get("/statements").json()["items"]
    assert [i["period_end"] for i in items] == sorted((i["period_end"] for i in items), reverse=True)
    assert all(set(i) == SUMMARY_KEYS | {"reconciliation"} for i in items)
    assert all("transactions" not in i for i in items)


def test_duplicate_statement(client):
    first = upload(client, VB_PDFS[0]).json()
    err = assert_error(upload(client, VB_PDFS[0]), 409, "duplicate")
    assert err["details"] == {"statement_id": first["id"]}
    assert len(client.get("/statements").json()["items"]) == 1


def test_unknown_format(client):
    resp = client.post("/statements", files={"file": ("x.pdf", blank_pdf(), "application/pdf")})
    assert_error(resp, 422, "unknown_format")


def test_not_a_pdf(client):
    resp = client.post("/statements", files={"file": ("x.pdf", b"GIF89a not a pdf", "application/pdf")})
    assert_error(resp, 415, "not_pdf")


def test_broken_pdf(client):
    resp = client.post("/statements", files={"file": ("x.pdf", b"%PDF-1.7\nbroken", "application/pdf")})
    assert_error(resp, 422, "invalid_pdf")


def test_upload_just_over_limit(client):
    data = b"%PDF-1.7\n" + b"0" * (10 * 1024 * 1024)
    err = assert_error(client.post("/statements", files={"file": ("x.pdf", data, "application/pdf")}), 413,
                       "file_too_large")
    assert err["details"]["max_bytes"] == 10 * 1024 * 1024


def test_upload_far_over_limit_is_rejected_before_parsing(client):
    data = b"%PDF-1.7\n" + b"0" * (11 * 1024 * 1024)
    assert_error(client.post("/statements", files={"file": ("x.pdf", data, "application/pdf")}), 413,
                 "file_too_large")


def test_missing_file(client):
    assert_error(client.post("/statements"), 422, "validation_error")


def test_delete_statement(client):
    st = upload(client, VB_PDFS[1]).json()
    assert client.delete(f"/statements/{st['id']}").status_code == 204
    assert_error(client.get(f"/statements/{st['id']}"), 404, "statement_not_found")
    assert_error(client.delete(f"/statements/{st['id']}"), 404, "statement_not_found")
    assert client.get("/transactions").json()["items"] == []
    assert upload(client, VB_PDFS[1]).status_code == 201  # no longer a duplicate


def test_unknown_route_uses_error_shape(client):
    assert_error(client.get("/nope"), 404, "not_found")
    assert_error(client.patch("/health"), 405, "method_not_allowed")


# -- manual entry ---------------------------------------------------------------------------------


def test_manual_transaction_lifecycle(client):
    body = {"date": "2026-09-10", "description": "Kitapçı", "amount": 12000, "currency": "TRY", "direction": "debit"}
    resp = client.post("/transactions", json=body)
    assert resp.status_code == 201, resp.text
    tx = resp.json()
    assert set(tx) >= TX_KEYS
    assert tx["source"] == "manual" and tx["installment"] is None and tx["kind"] == "purchase"
    assert client.get("/transactions", params={"source": "manual"}).json()["items"] == [tx]
    assert client.get("/transactions", params={"month": "2026-09"}).json()["items"] == [tx]
    assert client.get("/transactions", params={"month": "2026-10"}).json()["items"] == []
    assert client.delete(f"/transactions/{tx['id']}").status_code == 204
    assert_error(client.delete(f"/transactions/{tx['id']}"), 404, "transaction_not_found")


def test_manual_installment_joins_schedule(client):
    body = {"date": date.today().isoformat(), "description": "Buzdolabı", "amount": 300000, "currency": "TRY",
            "direction": "debit", "installment": {"total_count": 6, "amount_each": 50000}}
    tx = client.post("/transactions", json=body).json()
    assert tx["installment"]["remaining_count"] == 6
    plan_id = tx["installment"]["plan_id"]
    sched = client.get("/installments/schedule", params={"months": 12}).json()
    plan = next(p for p in sched["plans"] if p["plan_id"] == plan_id)
    assert plan["remaining_count"] == 6 and plan["total_remaining"] == 300000 and plan["breakdown"] is None
    assert sum(i["amount"] for m in sched["months"] for i in m["items"] if i["plan_id"] == plan_id) == 300000
    assert client.delete(f"/transactions/{tx['id']}").status_code == 204
    sched = client.get("/installments/schedule").json()
    assert all(p["plan_id"] != plan_id for p in sched["plans"])


def test_manual_validation(client):
    base = {"date": "2026-09-10", "description": "X", "amount": 100, "currency": "TRY", "direction": "credit"}
    assert_error(client.post("/transactions", json={**base, "installment": {"total_count": 3, "amount_each": 10}}),
                 422, "invalid_installment")
    assert_error(client.post("/transactions", json={**base, "amount": -5}), 422, "validation_error")
    assert_error(client.post("/transactions", json={**base, "currency": "tl"}), 422, "validation_error")
    assert_error(client.get("/transactions", params={"month": "2026-13"}), 422, "validation_error")
    assert_error(client.get("/transactions", params={"source": "bank"}), 422, "validation_error")


def test_manual_description_is_redacted(client):
    iban = "TR33 0006 1005 1978 6457 8413 26"
    body = {"date": "2026-09-10", "description": f"Havale {iban}", "amount": 100, "currency": "TRY",
            "direction": "debit"}
    tx = client.post("/transactions", json=body).json()
    assert "1978" not in tx["description"] and "[IBAN]" in tx["description"]


def test_statement_transaction_cannot_be_deleted(client):
    st = upload(client, VB_PDFS[2]).json()
    tx_id = st["transactions"][0]["id"]
    assert_error(client.delete(f"/transactions/{tx_id}"), 409, "not_manual")


def test_transactions_month_follows_statement_cut_off(client):
    st = upload(client, VB_PDFS[11]).json()
    month = st["period_end"][:7]
    items = client.get("/transactions", params={"month": month, "source": "statement"}).json()["items"]
    assert {t["id"] for t in items} == {t["id"] for t in st["transactions"]}
    assert [t["date"] for t in items] == sorted(t["date"] for t in items)


# -- calculations ---------------------------------------------------------------------------------


def test_schedule_matches_build_schedule(client):
    for path in VB_PDFS[:12]:
        assert upload(client, path).status_code == 201
    got = client.get("/installments/schedule", params={"months": 12}).json()
    expected = build_schedule([parse_pdf(p) for p in VB_PDFS[:12]], months=12).model_dump(mode="json")
    assert got == expected
    assert {"currency", "months", "plans"} <= set(got)
    assert len(got["months"]) == 12
    assert len(client.get("/installments/schedule", params={"months": 3}).json()["months"]) == 3
    assert_error(client.get("/installments/schedule", params={"months": 0}), 422, "validation_error")


def test_schedule_empty(client):
    got = client.get("/installments/schedule").json()
    assert got["currency"] == "TRY" and got["plans"] == [] and len(got["months"]) == 12
    assert all(m["total"] == 0 and m["items"] == [] for m in got["months"])


def test_summary_empty_month_is_zero_not_404(client):
    resp = client.get("/summary/monthly", params={"month": "2020-01"})
    assert resp.status_code == 200
    assert resp.json() == {"month": "2020-01", "currency": "TRY", "total_spend": 0, "total_credit": 0,
                           "by_category": [], "previous_month": None, "statement_ids": []}
    assert_error(client.get("/summary/monthly", params={"month": "2020-1"}), 422, "validation_error")


def test_summary_of_statement_months(rules_off, client):
    a = upload(client, VB_PDFS[10]).json()
    b = upload(client, VB_PDFS[11]).json()
    month = b["period_end"][:7]
    s = client.get("/summary/monthly", params={"month": month}).json()
    assert s["statement_ids"] == [b["id"]]
    spend = sum(t["amount"] for t in b["transactions"]
                if t["direction"] == "debit" and t["kind"] not in ("carry_over", "payment"))
    assert s["total_spend"] == spend > 0
    assert sum(c["amount"] for c in s["by_category"]) == spend
    assert s["by_category"][0]["category"] == "uncategorized" and s["by_category"][0]["share"] == "1.00"
    prev = client.get("/summary/monthly", params={"month": a["period_end"][:7]}).json()
    assert s["previous_month"]["total_spend"] == prev["total_spend"]
    assert isinstance(s["previous_month"]["change"], str)


def test_subscriptions_endpoint(client):
    for d, amount in (("2026-07-05", 9999), ("2026-08-05", 9999), ("2026-09-05", 11999)):
        client.post("/transactions", json={"date": d, "description": "MUZIK SERVISI", "amount": amount,
                                           "currency": "TRY", "direction": "debit"})
    items = client.get("/subscriptions").json()["items"]
    assert items == [{"merchant": "MUZIK SERVISI", "amount_last": 11999, "amount_previous": 9999, "currency": "TRY",
                      "months_seen": ["2026-07", "2026-08", "2026-09"], "price_change": "0.20"}]


# -- CORS and Private Network Access --------------------------------------------------------------


def test_cors_preflight_with_private_network(client):
    resp = client.options("/statements", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true",
    })
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert resp.headers["access-control-allow-private-network"] == "true"


def test_cors_exposes_headers_the_front_end_reads(client):
    resp = client.get("/health", headers={"Origin": "http://127.0.0.1:5173"})
    exposed = {h.strip().lower() for h in resp.headers["access-control-expose-headers"].split(",")}
    assert {"x-kalan-password-remembered", "x-kalan-skipped-rows", "content-disposition"} <= exposed


def test_cors_rejects_other_origins(client):
    resp = client.options("/statements", headers={
        "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-private-network" not in resp.headers
    simple = client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in simple.headers


def test_cors_simple_request(client):
    resp = client.get("/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert resp.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_cors_allowlist_from_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("KALAN_CORS_ORIGINS", "http://localhost:3000")
    monkeypatch.setenv("KALAN_DATA_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    c = TestClient(create_app(registry=ProviderRegistry([])))
    assert c.get("/health", headers={"Origin": "http://localhost:3000"}).headers[
        "access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": "http://localhost:5173"}).headers
    assert (tmp_path / "kalan.db").exists()


# -- analysis jobs --------------------------------------------------------------------------------


@pytest.fixture
def analysed_setup(client, fake):
    st = upload(client, VB_PDFS[11]).json()
    client.put("/providers/selected", json={"id": "fake"})
    return st


def run_job(client, statement_id: str) -> dict:
    resp = client.post(f"/statements/{statement_id}/analyze")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending" and body["job_id"].startswith("job_")
    client.app.state.runner.wait(body["job_id"], timeout=10)
    return client.get(f"/jobs/{body['job_id']}").json()


def test_analyze_without_provider(client):
    st = upload(client, VB_PDFS[0]).json()
    assert_error(client.post(f"/statements/{st['id']}/analyze"), 409, "no_provider")
    assert_error(client.post("/statements/st_missing/analyze"), 404, "statement_not_found")


def test_job_lifecycle_done(rules_off, client, fake, analysed_setup):
    st = analysed_setup
    assert_error(client.get(f"/statements/{st['id']}/analysis"), 404, "analysis_not_found")
    fake.answers = lambda p: answer_for(p, "groceries", recurring=True)
    job = run_job(client, st["id"])
    assert job["status"] == "done" and job["error"] is None
    assert len(fake.prompts) == 1
    analysis = client.get(f"/statements/{st['id']}/analysis").json()
    assert set(analysis) == {"categories_applied", "commentary", "provider", "created_at"}
    assert analysis["categories_applied"] is True and analysis["provider"] == "fake"
    assert analysis["commentary"]["locale"] == "tr"
    item = analysis["commentary"]["watch_items"][0]
    assert item["transaction_ids"] == [st["transactions"][0]["id"]]  # unknown id dropped
    again = client.get(f"/statements/{st['id']}").json()
    assert again["analysis_status"] == "done"
    assert {t["category"] for t in again["transactions"]} == {"groceries"}
    month = st["period_end"][:7]
    s = client.get("/summary/monthly", params={"month": month}).json()
    assert [c["category"] for c in s["by_category"]] == ["groceries"]


def test_job_pending_state(client, fake, analysed_setup):
    fake.answers = answer_for
    fake.release.clear()
    resp = client.post(f"/statements/{analysed_setup['id']}/analyze").json()
    assert client.get(f"/jobs/{resp['job_id']}").json()["status"] == "pending"
    assert client.get(f"/statements/{analysed_setup['id']}").json()["analysis_status"] == "pending"
    # A second request while pending returns the same job instead of a second model call.
    assert client.post(f"/statements/{analysed_setup['id']}/analyze").json()["job_id"] == resp["job_id"]
    fake.release.set()
    client.app.state.runner.wait(resp["job_id"], timeout=10)
    assert client.get(f"/jobs/{resp['job_id']}").json()["status"] == "done"
    assert len(fake.prompts) == 1


def test_job_retries_once_on_invalid_json(client, fake, analysed_setup):
    answers = iter(["Sure! Here is the analysis.", None])
    fake.answers = lambda p: next(answers) or answer_for(p)
    job = run_job(client, analysed_setup["id"])
    assert job["status"] == "done"
    assert len(fake.prompts) == 2 and "could not be used" in fake.prompts[1]


def test_job_fails_after_two_invalid_answers(client, fake, analysed_setup):
    fake.answers = ['{"categories": {}, "commentary": {"summary": "x"}}', '{"categories": "nope"}']
    job = run_job(client, analysed_setup["id"])
    assert job["status"] == "failed"
    assert "invalid answer twice" in job["error"]
    assert client.get(f"/statements/{analysed_setup['id']}").json()["analysis_status"] == "failed"
    assert len(fake.prompts) == 2


def test_job_fails_on_provider_error(client, fake, analysed_setup):
    fake.answers = ["!error:did not answer within 180 s"]
    job = run_job(client, analysed_setup["id"])
    assert job["status"] == "failed" and "180 s" in job["error"]
    assert len(fake.prompts) == 1


def test_job_rejects_unknown_category(client, fake, analysed_setup):
    fake.answers = lambda p: answer_for(p, "yachts")
    job = run_job(client, analysed_setup["id"])
    assert job["status"] == "failed" and "categories" in job["error"]


def test_unknown_job(client):
    assert_error(client.get("/jobs/job_missing"), 404, "job_not_found")


def test_interrupted_jobs_fail_on_restart(config, store, registry, client, fake, analysed_setup):
    fake.release.clear()
    job_id = client.post(f"/statements/{analysed_setup['id']}/analyze").json()["job_id"]
    create_app(config=config, store=store, registry=registry)  # a restart over the same database
    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "failed" and job["error"].startswith("interrupted")
    fake.release.set()
    client.app.state.runner.wait(job_id, timeout=10)


def test_payload_from_uploaded_pdf_has_no_personal_data(rules_off, client, fake, analysed_setup):
    fake.answers = answer_for
    assert run_job(client, analysed_setup["id"])["status"] == "done"
    prompt = fake.prompts[0]
    payload = payload_of(prompt)
    for row in payload["transactions"]:
        assert set(row) == {"id", "date", "description", "amount", "currency", "kind"}
    # Values the synthetic statement prints in its header (tools/synth_vakifbank.py).
    for secret in ("4799", "ORNEK KISI", "ORNEK MAH", "ORNEKKOY", "DENEME SOK", "1234567890",
                   "ORNEK SUBESI", "Kart No", "Müşteri"):
        assert secret not in prompt
