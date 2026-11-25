"""Anonymous diagnosis: structure only, no text and no amounts from the statement."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pymupdf
import pytest

from kalan.diagnose import as_text, diagnose, shape
from kalan.pdf import PasswordError
from tests.conftest import VB_PDFS

FIXTURES = Path(__file__).parent / "fixtures"
BANKS = {"vakifbank": "tr_vakifbank_worldcard", "yapikredi": "tr_yapikredi_world", "garanti": "tr_garanti_bonus"}


def test_shape():
    assert shape("Ödeme 1.234,56 TL") == "Aaaaa 9.999,99 AA"
    assert shape("ORNEK MARKET" + " " * 40) == "AAAAA AAAAAA<40 sp>"
    assert shape("İşlem ılık") == "Aaaaa aaaa"
    assert shape("   ") == "<3 sp>"
    assert shape(" ") == ""


@pytest.mark.parametrize("bank", BANKS)
def test_report_has_no_statement_text(bank):
    pdf = sorted((FIXTURES / bank).glob("*.pdf"))[3]
    truth = json.loads(pdf.with_suffix(".json").read_text(encoding="utf-8"))
    report = diagnose(pdf)
    shapes = re.sub(r"<\d+ sp>", " ", "\n".join(s["shape"] for s in report["shapes"]))
    assert not re.search(r"[0-8]", shapes)
    assert not re.search(r"[^\W\d_aA]", shapes)  # no letter but a / A
    # the template anchors are kalan's own text; outside them nothing of the statement appears
    blob = json.dumps({k: v for k, v in report.items() if k != "templates"}, ensure_ascii=False)
    for tx in truth["transactions"]:
        assert tx["description"] not in blob
    assert report["page_count"] == truth["statement"]["page_count"]
    assert report["has_text"] is True
    detected = [t["id"] for t in report["templates"] if t["detected"]]
    assert detected == [BANKS[bank]]
    own = next(t for t in report["templates"] if t["id"] == BANKS[bank])["anchors"]
    assert own["detect_all"]["found"] == own["detect_all"]["total"]
    assert own["table_header"]["missing"] == []
    assert own["table_end"] is True
    style = "1,234.56" if bank == "vakifbank" else "1.234,56"
    assert report["number_styles"][style] > 0
    text = as_text(report)
    assert f"template {BANKS[bank]}: detected=yes" in text
    assert not any(tx["description"] in text for tx in truth["transactions"])


def test_scanned_pdf_has_no_text():
    doc = pymupdf.open()
    doc.new_page()
    report = diagnose(doc.tobytes())
    assert report["has_text"] is False
    assert report["page_count"] == 1
    assert not any(t["detected"] for t in report["templates"])


def test_encrypted():
    with pymupdf.open(str(VB_PDFS[0])) as doc:
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="ornek-1", owner_pw="ornek-2")
    with pytest.raises(PasswordError):
        diagnose(data)
    assert diagnose(data, password="ornek-1")["page_count"] == diagnose(VB_PDFS[0])["page_count"]


def test_api(client):
    pdf = VB_PDFS[1]
    r = client.post("/diagnose", files={"file": ("x.pdf", pdf.read_bytes(), "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    assert body["report"]["templates"][1]["id"] == "tr_vakifbank_worldcard"
    assert body["text"].startswith("kalan ")
    assert client.get("/statements").json()["items"] == []  # nothing stored
    r = client.post("/diagnose", files={"file": ("x.pdf", b"hello", "application/pdf")})
    assert r.status_code == 415
    with pymupdf.open(str(pdf)) as doc:
        locked = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="ornek-1", owner_pw="ornek-2")
    r = client.post("/diagnose", files={"file": ("x.pdf", locked, "application/pdf")})
    assert r.json()["error"]["code"] == "pdf_password_required"
    r = client.post("/diagnose", files={"file": ("x.pdf", locked, "application/pdf")}, data={"password": "ornek-1"})
    assert r.status_code == 200
