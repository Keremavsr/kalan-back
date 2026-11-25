"""hledger, beancount, OFX and CSV export; OFX and CSV read back through kalan's own importers."""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from datetime import date

from kalan.imports import parse_import
from kalan.insights.export import export, export_rows, to_beancount, to_hledger
from kalan.models import InstallmentInfo
from kalan.schedule import build_schedule
from tests.insights_data import statement, tx


def sample():
    info = InstallmentInfo(remaining_count=4, remaining_amount_each=10000, is_last=False, plan_id="pl_x", index=2,
                           total_count=6)
    return statement(period_end=date(2099, 1, 15), transactions=[
        tx("c0", date(2099, 1, 1), 99999, kind="carry_over", description="ONCEKI DONEM BAKIYESI"),
        tx("t1", date(2099, 1, 3), 123456, description="ORNEK MARKET, SUBE \"A\""),
        tx("t2", date(2099, 1, 4), 10000, kind="installment", description="ORNEK ELEKTRONIK", installment=info),
        tx("t3", date(2099, 1, 5), 2500, kind="refund", direction="credit", description="ORNEK IADE"),
        tx("t4", date(2099, 1, 9), 50000, kind="payment", direction="credit", description="ODEME TESEKKURLER"),
        tx("t5", date(2099, 1, 10), 1999, description="ORNEK USD", currency="USD"),
    ])


def plans():
    return build_schedule([sample()], months=12).plans


def key(t):
    return (t.date, t.amount, t.direction, t.currency)


def test_csv_round_trip():
    text, skipped = export("csv", [sample()], [], plans(), "TRY")
    assert skipped == 0 and "ONCEKI" not in text
    back = parse_import(text.encode("utf-8"), "kalan.csv", None, "TRY")
    exported = [t for t, _ in export_rows([sample()], [])]
    assert sorted(key(t) for t in back.transactions) == sorted(key(t) for t in exported)
    assert {t.description for t in back.transactions} == {t.description for t in exported}
    inst = next(t for t in back.transactions if t.installment)
    assert (inst.installment.index, inst.installment.total_count) == (2, 6)
    assert back.detected["sign"] == "positive_debit"


def test_ofx_round_trip():
    text, skipped = export("ofx", [sample()], [], plans(), "TRY")
    assert skipped == 1  # the USD row
    ET.fromstring(text.split("?>\n", 2)[2])  # well-formed XML body
    back = parse_import(text.encode("utf-8"), "kalan.ofx", None, "TRY")
    exported = [t for t, _ in export_rows([sample()], []) if t.currency == "TRY"]
    assert sorted(key(t) for t in back.transactions) == sorted(key(t) for t in exported)
    inst = next(t for t in back.transactions if t.installment)
    assert inst.description == "ORNEK ELEKTRONIK 2/6" and inst.installment.index == 2
    again = parse_import(text.encode("utf-8"), "kalan.ofx", None, "TRY")
    assert [t.id for t in again.transactions] == [t.id for t in back.transactions]  # FITID keeps ids stable


def test_hledger_journal():
    text = to_hledger(export_rows([sample()], []), plans(), "TRY")
    assert "2099-01-03 ORNEK MARKET" in text and "    expenses:purchase  1234.56 TRY" in text
    assert "    assets:payments  -500.00 TRY" in text and "liabilities:card:ornekbank:0000" in text
    assert "~ monthly from 2099-02-01 to 2099-06-01  ; plan:pl_x" in text
    assert "    expenses:installments  100.00 TRY" in text
    assert "ONCEKI" not in text


def test_beancount():
    text = to_beancount(export_rows([sample()], []), plans(), "TRY")
    assert '2099-01-03 * "ORNEK MARKET, SUBE \\"A\\""' in text
    assert "2099-01-01 open Liabilities:Card:Ornekbank:X0000" not in text
    assert "open Liabilities:Card:Ornekbank:X0000" in text and "open Expenses:Purchase" in text
    assert "  Expenses:Refund  -25.00 TRY" in text
    assert '2099-02-01 custom "kalan-plan" "pl_x" "ORNEK ELEKTRONIK" 100.00 TRY 4' in text


def test_plans_csv_and_route(client, store):
    store.add_statement(sample())
    r = client.get("/insights/export", params={"format": "csv", "what": "plans"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows == [{"plan_id": "pl_x", "description": "ORNEK ELEKTRONIK", "kind": "purchase", "source": "statement",
                     "amount_each": "100.00", "remaining_count": "4", "ends": "2099-05", "total_remaining": "400.00",
                     "currency": "TRY"}]
    ofx = client.get("/insights/export", params={"format": "ofx"})
    assert ofx.headers["x-kalan-skipped-rows"] == "1" and "kalan-transactions.ofx" in ofx.headers["content-disposition"]
    assert client.get("/insights/export", params={"format": "ofx", "what": "plans"}).status_code == 422
    assert client.get("/insights/export", params={"format": "xls"}).status_code == 422
    assert "ORNEK MARKET" in client.get("/insights/export", params={"format": "beancount"}).text
