"""AI fallback reader and template drafts, with the fake adapter and invented statements only."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from kalan.ai.adapters import FakeAdapter
from kalan.ai.reader import (
    PARSER_ID,
    InvalidModelOutputError,
    TextTooLongError,
    build_reader_prompt,
    draft_template,
    parse_reader_answer,
    read_statement,
)
from kalan.amounts import decimal_to_minor, minor_units

HERE = Path(__file__).parent / "fixtures" / "templates"
TEXT = (HERE / "br_bancoexemplo_ouro.txt").read_text(encoding="utf-8")
TEMPLATE_YAML = (HERE / "br_bancoexemplo_ouro.yaml").read_text(encoding="utf-8")
MASKED = TEXT.replace("[CARD ****4321]", "5555 55** **** 4321")  # as a bank prints it (invented number)


def answer(**changes) -> dict:
    rows = [
        {"date": "2026-02-02", "description": "SALDO ANTERIOR", "amount": "500.00", "sign": "debit",
         "kind": "carry_over"},
        {"date": "2026-02-05", "description": "PAGAMENTO RECEBIDO", "amount": "500.00", "sign": "credit",
         "kind": "payment"},
        {"date": "2026-02-08", "description": "MERCADO CENTRAL", "amount": "234.56", "sign": "debit",
         "kind": "purchase"},
        {"date": "2026-02-10", "description": "LOJA DE MOVEIS", "amount": "300.00", "sign": "debit",
         "kind": "purchase", "installment": {"index": 3, "count": 10}},
        {"date": "2026-02-12", "description": "LIVRARIA AURORA", "amount": "100.00", "sign": "debit",
         "kind": "installment", "installment": {"index": 10, "count": 10}},
        {"date": "2026-02-15", "description": "APP STREAMING 12,00 USD", "amount": "60.00", "sign": "debit",
         "kind": "purchase", "original_amount": "12.00", "original_currency": "usd"},
        {"date": "2026-02-20", "description": "ESTORNO LIVRARIA", "amount": "40.00", "sign": "credit",
         "kind": "refund"},
        {"date": "2026-02-25", "description": "ANUIDADE", "amount": "30.00", "sign": "debit", "kind": "fee"},
        {"date": "2026-02-26", "description": "IOF COMPRA EXTERIOR", "amount": "10.00", "sign": "debit",
         "kind": "tax"},
    ]
    data = {
        "bank": "Banco Exemplo", "product": "Ouro", "country": "BR", "currency": "BRL",
        "period_start": None, "period_end": "2026-03-01", "due_date": "2026-03-10",
        "statement_total": "694.56", "minimum_payment": "104.18", "credit_limit": "8000.00",
        "rows": rows,
        "printed_totals": {"previous_balance": "500.00", "period_transactions": "664.56",
                           "interest_and_fees": "30.00", "payments": "500.00", "statement_total": "694.56"},
    }
    data.update(changes)
    return data


def pdf_from_text(text: str) -> bytes:
    doc = pymupdf.open()
    for page_text in text.split("\f"):
        page = doc.new_page()
        y = 40
        for line in page_text.splitlines():
            page.insert_text((40, y), line, fontsize=9)
            y += 12
    return doc.tobytes()


# -- code part ------------------------------------------------------------------------------------


def test_minor_units_any_iso_currency():
    assert (minor_units("TRY"), minor_units("JPY"), minor_units("KWD"), minor_units("ILS")) == (2, 0, 3, 2)
    assert decimal_to_minor("-12.30", "BRL") == -1230
    assert decimal_to_minor("1500", "JPY") == 1500
    assert decimal_to_minor("1.234", "KWD") == 1234
    for bad in ("12,30", "1.5", "abc"):
        currency = "JPY" if bad == "1.5" else "BRL"
        with pytest.raises(ValueError):
            decimal_to_minor(bad, currency)
    with pytest.raises(ValueError):
        minor_units("real")


def test_answer_becomes_reconciled_statement():
    st = parse_reader_answer(json.dumps(answer()), card_last4="4321")
    assert st.parser_id == PARSER_ID and st.bank == "bancoexemplo" and st.currency == "BRL"
    assert (st.statement_total, st.minimum_payment, st.credit_limit) == (69456, 10418, 800000)
    assert st.period_start.isoformat() == "2026-02-03"
    assert st.reconciliation.status == "ok" and st.needs_review is False
    moveis = st.transactions[3]
    assert moveis.kind == "installment"
    assert (moveis.installment.remaining_count, moveis.installment.remaining_amount_each,
            moveis.installment.total_count, moveis.installment.is_last) == (7, 30000, 10, False)
    assert st.transactions[4].installment.is_last is True
    app = st.transactions[5]
    assert (app.original_amount, app.original_currency) == (1200, "USD")
    assert all(t.currency == "BRL" for t in st.transactions)


def test_mismatch_needs_review():
    data = answer()
    data["rows"][2]["amount"] = "243.56"  # a misread digit
    st = parse_reader_answer(json.dumps(data), card_last4=None)
    assert st.reconciliation.status == "mismatch" and st.needs_review is True
    assert st.reconciliation.difference == 900


def test_plan_id_stable_next_month():
    a = parse_reader_answer(json.dumps(answer()), "4321").transactions[3].installment.plan_id
    nxt = answer(period_end="2026-04-01", due_date="2026-04-10")
    nxt["rows"][3]["installment"] = {"index": 4, "count": 10}
    b = parse_reader_answer(json.dumps(nxt), "4321").transactions[3].installment.plan_id
    assert a == b


@pytest.mark.parametrize(
    "bad",
    [
        "no json here",
        json.dumps(answer(currency="reais")),
        json.dumps(answer(statement_total="694,56")),
        json.dumps({k: v for k, v in answer().items() if k != "rows"}),
    ],
)
def test_invalid_answers(bad):
    with pytest.raises(InvalidModelOutputError):
        parse_reader_answer(bad, None)


def test_prompt_is_redacted_and_retry_once():
    leaky = TEXT.replace("[CARD ****4321]", "4111 1111 1111 1111") + "\nIBAN TR33 0006 1005 1978 6457 8413 26\n"
    fake = FakeAdapter(["not json", json.dumps(answer())])
    st = read_statement(fake, leaky, card_last4="1111")
    assert st.reconciliation.status == "ok"
    assert len(fake.prompts) == 2
    for p in fake.prompts:
        assert "4111 1111" not in p and "TR33" not in p
    with pytest.raises(TextTooLongError):
        read_statement(FakeAdapter([]), "x " * 40_000, None)


def test_reader_prompt_asks_for_transcription_not_math():
    p = build_reader_prompt("TEXT")
    assert "Do not compute" in p and "ONE JSON object" in p and p.endswith("TEXT")


def test_draft_template_valid_and_checked():
    result = draft_template(FakeAdapter(["```yaml\n" + TEMPLATE_YAML + "```"]), TEXT)
    assert result["valid"] is True and result["errors"] == [] and result["detect"] is True
    assert result["reconciliation"]["status"] == "ok"
    assert result["yaml"].startswith("# Invented layout")


def test_draft_template_invalid_twice():
    fake = FakeAdapter(["id: [", "id: Not-Valid\n"])
    result = draft_template(fake, TEXT)
    assert result["valid"] is False and result["errors"]
    assert "rejected" in fake.prompts[1]


# -- API ------------------------------------------------------------------------------------------


def post_pdf(client, data: bytes, **params):
    return client.post("/statements", params=params, files={"file": ("s.pdf", data, "application/pdf")})


def select_fake(client):
    assert client.put("/providers/selected", json={"id": "fake"}).status_code == 200


def test_api_unknown_format_without_provider(client):
    resp = post_pdf(client, pdf_from_text(TEXT))
    assert resp.status_code == 422
    assert resp.json()["error"]["details"] == {"ai_fallback": "no_provider"}


def test_api_fallback_off(client, fake):
    select_fake(client)
    resp = post_pdf(client, pdf_from_text(TEXT), ai_fallback="false")
    assert resp.status_code == 422 and resp.json()["error"]["details"] == {"ai_fallback": "off"}
    assert fake.prompts == []


def test_api_fallback_reads_stores_and_drafts(client, fake, store):
    select_fake(client)
    fake.answers = [json.dumps(answer())]
    resp = post_pdf(client, pdf_from_text(MASKED))
    assert resp.status_code == 201, resp.text
    st = resp.json()
    assert st["parser_id"] == "ai-fallback" and st["needs_review"] is False
    assert st["reconciliation"]["status"] == "ok" and st["card_last4"] == "4321"
    assert "SALDO ANTERIOR" in fake.prompts[0] and "5555 55" not in fake.prompts[0]
    listed = client.get("/statements").json()["items"][0]
    assert listed["needs_review"] is False
    assert st["analysis_job_id"]  # a provider is selected: the new statement joins the AI batch
    client.app.state.runner.wait(st["analysis_job_id"], 20)

    fake.answers = [TEMPLATE_YAML]
    draft = client.post(f"/statements/{st['id']}/template-draft")
    assert draft.status_code == 200, draft.text
    body = draft.json()
    assert body["provider"] == "fake" and body["valid"] is True and body["detect"] is True
    assert body["reconciliation"]["status"] == "ok"

    assert client.delete(f"/statements/{st['id']}").status_code == 204
    assert store.statement_text(st["id"]) is None


def test_api_fallback_mismatch_flagged(client, fake):
    select_fake(client)
    data = answer()
    data["rows"].pop(2)
    fake.answers = [json.dumps(data)]
    st = post_pdf(client, pdf_from_text(TEXT)).json()
    assert st["needs_review"] is True and st["reconciliation"]["status"] == "mismatch"


def test_api_fallback_provider_errors(client, fake):
    select_fake(client)
    fake.answers = ["!error:quota reached"]
    resp = post_pdf(client, pdf_from_text(TEXT))
    assert resp.status_code == 502 and resp.json()["error"]["code"] == "ai_fallback_failed"
    fake.answers = ["nope", "still nope"]
    resp = post_pdf(client, pdf_from_text(TEXT))
    assert resp.status_code == 502 and "invalid answer twice" in resp.json()["error"]["message"]


def test_api_template_draft_needs_fallback_text(client, fake):
    vb = sorted((Path(__file__).parent / "fixtures" / "vakifbank").glob("vb-*.pdf"))[2]
    st = client.post("/statements", files={"file": (vb.name, vb.read_bytes(), "application/pdf")}).json()
    resp = client.post(f"/statements/{st['id']}/template-draft")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "no_statement_text"
    assert client.post("/statements/st_missing/template-draft").status_code == 404
