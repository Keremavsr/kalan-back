"""CSV / OFX / QIF import with invented rows only."""

from __future__ import annotations

import json
from datetime import date

import pytest

from kalan.imports import (
    ImportFileError,
    Mapping,
    detect_amount_format,
    detect_date_format,
    import_plans,
    parse_import,
    sniff_format,
)
from kalan.schedule import build_schedule

BR_CSV = """Fatura do cartão (exemplo inventado)
data;lançamento;valor
05/02/2026;MERCADO CENTRAL;1.234,56
10/02/2026;LOJA DE MOVEIS - Parcela 3/10;300,00
12/02/2026;PAGAMENTO RECEBIDO;-500,00
15/02/2026;ESTORNO LIVRARIA;-40,00
25/02/2026;ANUIDADE;30,00
"""

US_CSV = """Transaction Date,Description,Amount
02/13/2026,COFFEE CORNER,-4.50
02/14/2026,ONLINE BOOKS,-12.00
02/20/2026,PAYMENT THANK YOU,100.00
"""

MX_CSV = """Fecha,Concepto,Cargo,Abono,Cuotas
2026-02-03,TIENDA DEMO,"1,200.00",,3 de 6
2026-02-04,SUPER EJEMPLO,250.50,,
2026-02-09,PAGO RECIBIDO,,"2,000.00",
"""

OFX = """OFXHEADER:100
DATA:OFXSGML
<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><CCSTMTRS><CURDEF>ILS
<BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260203120000<TRNAMT>-89.90<FITID>A1<NAME>CAFE DUGMA
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260205<TRNAMT>-300.00<FITID>A2<NAME>CHANUT 2/5<MEMO>installment
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260210<TRNAMT>150.00<FITID>A3<NAME>PAYMENT
</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>
"""

OFX_XML = """<?xml version="1.0"?><OFX><CCSTMTRS><CURDEF>USD</CURDEF><BANKTRANLIST>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260301</DTPOSTED><TRNAMT>-20.00</TRNAMT><FITID>X1</FITID>
<NAME>SAMPLE STORE</NAME></STMTTRN></BANKTRANLIST></CCSTMTRS></OFX>"""

QIF = """!Type:CCard
D03/02/2026
T-45.00
PPADARIA MODELO
^
D04/02'26
T-120.00
PLOJA EXEMPLO
M2/4
^
"""


def test_detect_amount_and_date_formats():
    assert detect_amount_format(["1.234,56", "300,00", "-40,00"])[0] == "comma_decimal"
    assert detect_amount_format(["1,234.56", "-4.50"])[0] == "dot_decimal"
    assert detect_amount_format(["1 234,56", "12,00"])[0] == "space_comma"
    assert detect_amount_format(["12", "1500"])[1].decimal == "."
    with pytest.raises(ImportFileError):
        detect_amount_format(["12,5x"])
    assert detect_date_format(["13/02/2026", "01/03/2026"]) == ("D/M/YYYY", False)
    assert detect_date_format(["02/13/2026"]) == ("M/D/YYYY", False)
    assert detect_date_format(["01/02/2026"]) == ("D/M/YYYY", True)
    assert detect_date_format(["2026-02-03T10:00:00"]) == ("YYYY-M-D", False)
    assert detect_date_format(["01/02/2026"], prefer="M/D/YYYY") == ("M/D/YYYY", False)


def test_brazil_csv_semicolon_comma_decimal_installment_from_description():
    r = parse_import(BR_CSV.encode("utf-8"), "fatura.csv", None, "BRL")
    d = r.detected
    assert (d["delimiter"], d["decimal"], d["date_format"], d["sign"]) == (";", ",", "D/M/YYYY", "positive_debit")
    assert d["columns"] == {"date": "data", "description": "lançamento", "amount": "valor"}
    kinds = [(t.kind, t.direction, t.amount) for t in r.transactions]
    assert kinds == [("purchase", "debit", 123456), ("installment", "debit", 30000), ("payment", "credit", 50000),
                     ("refund", "credit", 4000), ("fee", "debit", 3000)]
    inst = r.transactions[1].installment
    assert (inst.index, inst.total_count, inst.remaining_count, inst.remaining_amount_each) == (3, 10, 7, 30000)
    assert all(t.source == "import" and t.currency == "BRL" for t in r.transactions)


def test_us_csv_negative_charges():
    r = parse_import(US_CSV.encode(), "activity.csv", None, "USD")
    assert r.detected["sign"] == "negative_debit" and r.detected["date_format"] == "M/D/YYYY"
    assert [(t.direction, t.amount, t.kind) for t in r.transactions] == [
        ("debit", 450, "purchase"), ("debit", 1200, "purchase"), ("credit", 10000, "payment")]


def test_mexico_csv_debit_credit_columns_and_installment_column():
    r = parse_import(MX_CSV.encode(), "movimientos.csv", Mapping(currency="MXN"), "TRY")
    assert r.currency == "MXN" and r.detected["delimiter"] == ","
    t = r.transactions
    assert [(x.direction, x.amount) for x in t] == [("debit", 120000), ("debit", 25050), ("credit", 200000)]
    assert (t[0].kind, t[0].installment.index, t[0].installment.total_count) == ("installment", 3, 6)
    assert t[2].kind == "payment"


def test_mapping_overrides_and_errors():
    csv = "when;what;how much\n01/02/2026;A STORE;10,00\n"
    with pytest.raises(ImportFileError, match="header"):
        parse_import(csv.encode(), "x.csv", None, "EUR")
    r = parse_import(csv.encode(), "x.csv", Mapping(date="when", description="what", amount="how much",
                                                    date_format="M/D/YYYY", sign="negative_debit"), "EUR")
    t = r.transactions[0]
    assert (t.date.isoformat(), t.direction, t.amount) == ("2026-01-02", "credit", 1000)
    with pytest.raises(ImportFileError, match="not in the header"):
        parse_import(csv.encode(), "x.csv", Mapping(date="when", description="nope", amount="how much"), "EUR")


def test_zero_decimal_currency():
    csv = "Date,Description,Amount\n2026-02-01,RAMEN DEMO,1200\n"
    t = parse_import(csv.encode(), "x.csv", Mapping(currency="JPY"), "TRY").transactions[0]
    assert (t.amount, t.currency) == (1200, "JPY")


def test_template_rules_apply(monkeypatch):
    csv = ("Tarih;Açıklama;Tutar\n03.02.2026;ORNEK MAGAZA 2/6;100,00\n"
           "04.02.2026;ODEMENIZ ICIN TESEKKURLER;-50,00\n05.02.2026;YILLIK KART AIDATI;10,00\n")
    r = parse_import(csv.encode(), "x.csv", Mapping(template="tr_vakifbank_worldcard"), "TRY")
    assert [t.kind for t in r.transactions] == ["installment", "payment", "fee"]
    assert r.transactions[0].installment.index == 2
    with pytest.raises(ImportFileError, match="no template"):
        parse_import(csv.encode(), "x.csv", Mapping(template="xx_none_none"), "TRY")


def test_ofx_sgml_and_xml():
    r = parse_import(OFX.encode(), "card.ofx", None, "TRY")
    assert r.format == "ofx" and r.currency == "ILS"
    assert [(t.direction, t.amount, t.kind) for t in r.transactions] == [
        ("debit", 8990, "purchase"), ("debit", 30000, "installment"), ("credit", 15000, "payment")]
    assert r.transactions[1].description == "CHANUT 2/5 installment"
    assert r.transactions[1].installment.total_count == 5
    again = parse_import(OFX.encode(), "card.ofx", None, "TRY")
    assert [t.id for t in again.transactions] == [t.id for t in r.transactions]
    x = parse_import(OFX_XML.encode(), "x.qfx", None, "TRY")
    assert (x.currency, x.transactions[0].amount, x.transactions[0].date.isoformat()) == ("USD", 2000, "2026-03-01")


def test_qif():
    r = parse_import(QIF.encode(), "card.qif", Mapping(currency="BRL"), "TRY")
    assert r.format == "qif" and [t.amount for t in r.transactions] == [4500, 12000]
    assert r.transactions[1].date.isoformat() == "2026-02-04"
    assert r.transactions[1].installment.index == 2


def test_sniff_format():
    assert sniff_format("a.txt", OFX.encode()) == "ofx"
    assert sniff_format(None, QIF.encode()) == "qif"
    assert sniff_format("a.csv", b"a,b") == "csv"


def test_import_plans_feed_schedule():
    feb = parse_import(BR_CSV.encode(), "f.csv", None, "BRL").transactions
    mar = parse_import(BR_CSV.replace("10/02/2026;LOJA DE MOVEIS - Parcela 3/10", "10/03/2026;LOJA DE MOVEIS - "
                                      "Parcela 4/10").encode(), "m.csv", None, "BRL").transactions
    plans = import_plans(feb + mar)
    assert len(plans) == 1 and plans[0].total_count == 10 and plans[0].start_date.isoformat() == "2025-12-10"
    sched = build_schedule([], imported=plans, start_month="2026-04", months=3)
    assert [m.total for m in sched.months] == [30000, 30000, 30000]
    assert sched.plans[0].source == "import" and sched.plans[0].remaining_count == 6


# -- API ------------------------------------------------------------------------------------------


def post(client, body: str, name: str, **form):
    return client.post("/imports", files={"file": (name, body.encode("utf-8"), "text/csv")}, data=form)


def test_api_import_roundtrip(client):
    assert client.put("/settings", json={"base_currency": "BRL"}).status_code == 200
    resp = post(client, BR_CSV, "fatura.csv", mapping=json.dumps({"currency": "BRL"}))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["rows"], body["imported"], body["skipped_duplicates"]) == (5, 5, 0)
    assert body["months"] == ["2026-02"] and body["detected"]["decimal"] == ","
    again = post(client, BR_CSV, "fatura.csv", mapping=json.dumps({"currency": "BRL"})).json()
    assert (again["imported"], again["skipped_duplicates"]) == (0, 5)

    listed = client.get("/transactions", params={"source": "import"}).json()["items"]
    assert len(listed) == 5 and all(t["source"] == "import" for t in listed)
    summary = client.get("/summary/monthly", params={"month": "2026-02"}).json()
    assert summary["total_spend"] > 0
    today = date.today()
    running = "Date,Description,Amount\n" + f"{today.isoformat()},SOFA DEMO 1/12,-50.00\n"
    assert post(client, running, "r.csv").status_code == 201
    sched = client.get("/installments/schedule", params={"months": 12}).json()
    plan = next(p for p in sched["plans"] if p["source"] == "import")
    assert (plan["amount_each"], plan["remaining_count"]) == (5000, 12)  # window starts this month
    assert len(client.get("/imports").json()["items"]) == 3

    assert client.delete(f"/imports/{body['id']}").status_code == 204
    left = client.get("/transactions", params={"source": "import"}).json()["items"]
    assert [t["description"] for t in left] == ["SOFA DEMO 1/12"]
    assert client.delete(f"/imports/{body['id']}").status_code == 404


def test_api_import_errors(client):
    bad = post(client, "just text\n", "x.csv")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "import_unreadable"
    m = post(client, BR_CSV, "x.csv", mapping=json.dumps({"colour": "red"}))
    assert m.status_code == 422 and m.json()["error"]["code"] == "invalid_mapping"
    m = post(client, BR_CSV, "x.csv", mapping="{not json")
    assert m.status_code == 422 and m.json()["error"]["code"] == "invalid_mapping"


def test_api_import_redacts_descriptions(client):
    csv = "Date,Description,Amount\n2026-02-01,TRANSFER TR33 0006 1005 1978 6457 8413 26,-10.00\n"
    tx = post(client, csv, "x.csv").json()["transactions"][0]
    assert "TR33" not in tx["description"]
