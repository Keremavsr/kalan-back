"""CLI for task 5a: ``import --password``, ``teshis`` and ``kategori``; the save gate and rules on CLI imports."""

from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pytest

from kalan import gate, pdf_passwords
from kalan.cli import main
from kalan.config import Config
from kalan.models import Transaction
from kalan.pipeline import parse_pdf
from kalan.reconcile import reconcile
from kalan.service import Kalan
from kalan.store import Store
from tests.conftest import VB_PDFS

PASSWORD = "ornek-9999"  # invented


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    monkeypatch.setenv("KALAN_LANG", "en")


@pytest.fixture
def kalan(tmp_path) -> Kalan:
    config = Config(data_dir=tmp_path)
    return Kalan(config=config, store=Store(config.db_path))


@pytest.fixture
def locked(tmp_path) -> Path:
    path = tmp_path / "locked.pdf"
    with pymupdf.open(str(VB_PDFS[0])) as doc:
        path.write_bytes(doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=PASSWORD,
                                     owner_pw=PASSWORD + "-owner"))
    return path


def run(kalan: Kalan, capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv), kalan=kalan)
    out, err = capsys.readouterr()
    return code, out, err


def error_code(kalan: Kalan, capsys, *argv: str) -> str:
    code, out, _ = run(kalan, capsys, *argv, "--json")
    assert code == 1
    return json.loads(out)["error"]["code"]


# -- import --password --------------------------------------------------------------------------------------


def test_import_encrypted_pdf(kalan, capsys, locked):
    assert error_code(kalan, capsys, "import", str(locked), "--no-ai") == "pdf_password_required"
    code, _, err = run(kalan, capsys, "--lang", "tr", "import", str(locked), "--no-ai")
    assert code == 1 and "PDF şifreli" in err and "--sifre" in err and "(pdf_password_required)" in err
    assert error_code(kalan, capsys, "import", str(locked), "--password", "yanlis") == "pdf_password_wrong"
    code, _, err = run(kalan, capsys, "--lang", "tr", "import", str(locked), "--sifre", "yanlis")
    assert code == 1 and "Parola bu PDF'i açmıyor" in err
    code, out, err = run(kalan, capsys, "import", str(locked), "--password", PASSWORD, "--no-ai")
    assert code == 0, err
    assert out.startswith("Stored statement st_")
    assert PASSWORD not in out + err


def test_import_password_prompt(kalan, capsys, locked, monkeypatch):
    asked = []
    monkeypatch.setattr("getpass.getpass", lambda prompt: asked.append(prompt) or PASSWORD)
    code, _, err = run(kalan, capsys, "import", str(locked), "--password", "--no-ai")
    assert code == 0, err
    assert asked == ["PDF password: "]


def test_import_uses_a_remembered_password(kalan, capsys, locked):
    pdf_passwords.remember(parse_pdf(VB_PDFS[0]).bank, PASSWORD)
    code, _, err = run(kalan, capsys, "import", str(locked), "--no-ai")
    assert code == 0, err


def test_password_hints_are_printed(kalan, capsys, locked, monkeypatch):
    monkeypatch.setattr(Kalan, "password_hints", lambda self: [{"bank": "ornekbank", "hint": "invented hint"}])
    code, _, err = run(kalan, capsys, "import", str(locked), "--no-ai")
    assert code == 1
    assert "ornekbank: invented hint" in err


# -- save gate and rules on CLI imports ---------------------------------------------------------------------


def test_cli_import_goes_through_the_save_gate(kalan, capsys, monkeypatch):
    real = gate.check

    def scaled(st, stored=()):
        bad = st.model_copy(update={"statement_total": st.statement_total * 1000})
        return real(bad.model_copy(update={"reconciliation": reconcile(bad)}), stored)

    monkeypatch.setattr(gate, "check", scaled)
    code, out, err = run(kalan, capsys, "import", str(VB_PDFS[2]), "--no-ai")
    assert code == 1
    assert "(save_blocked)" in err and "amount_scale:" in err
    assert kalan.statements() == []

    monkeypatch.setattr(gate, "check", lambda st, stored=(): real(st.model_copy(update={"printed_summary": None}),
                                                                  stored))
    code, out, err = run(kalan, capsys, "import", str(VB_PDFS[2]), "--no-ai")
    assert code == 0, err
    assert "needs review" in out and "to review: no_printed_summary" in out
    assert kalan.statements()[0].needs_review is True


def test_cli_import_duplicate_still_409_code(kalan, capsys):
    assert run(kalan, capsys, "import", str(VB_PDFS[0]), "--no-ai")[0] == 0
    assert error_code(kalan, capsys, "import", str(VB_PDFS[0]), "--no-ai") == "duplicate"


def _merchant_tx(path: Path) -> tuple[Transaction, str]:
    """A purchase row of the fixture and a phrase of its first two words."""
    st = parse_pdf(path)
    tx = next(t for t in st.transactions if t.direction == "debit" and t.kind == "purchase")
    return tx, " ".join(tx.description.split()[:2])


def test_user_rule_applies_to_later_imports(kalan, capsys):
    tx, phrase = _merchant_tx(VB_PDFS[1])
    kalan.add_category_rule(phrase, "education")
    assert run(kalan, capsys, "import", str(VB_PDFS[1]), "--no-ai")[0] == 0
    stored = kalan.statement(kalan.statements()[0].id)
    assert next(t for t in stored.transactions if t.id == tx.id).category == "education"


# -- kategori -----------------------------------------------------------------------------------------------


def test_rules_list_add_delete(kalan, capsys):
    code, out, _ = run(kalan, capsys, "kategori")
    assert code == 0 and out.startswith("No category rules yet.")
    assert run(kalan, capsys, "import", str(VB_PDFS[1]), "--no-ai")[0] == 0
    _, phrase = _merchant_tx(VB_PDFS[1])

    code, out, err = run(kalan, capsys, "--lang", "tr", "kategori", "--ekle", phrase, "eğitim")
    assert code == 0, err
    assert "eklendi" in out and "→ eğitim" in out
    assert "satırın kategorisi değişti" in out
    rules = json.loads(run(kalan, capsys, "categories", "--json")[1])
    assert [r["category"] for r in rules["items"]] == ["education"]
    assert "groceries" in rules["categories"]
    rule_id = rules["items"][0]["id"]

    code, out, _ = run(kalan, capsys, "kategori")
    assert rule_id in out and "education" in out
    assert error_code(kalan, capsys, "kategori", "--add", phrase.lower(), "groceries") == "duplicate_rule"
    assert error_code(kalan, capsys, "kategori", "--add", "ORNEK MARKET", "ornek-kategori") == "invalid_category"
    assert error_code(kalan, capsys, "kategori", "--add", "***", "groceries") == "invalid_pattern"

    code, out, _ = run(kalan, capsys, "kategori", "--delete", rule_id)
    assert code == 0 and rule_id in out
    assert kalan.category_rules()["items"] == []
    assert error_code(kalan, capsys, "kategori", "--sil", rule_id) == "category_rule_not_found"


def test_rule_without_apply(kalan, capsys):
    assert run(kalan, capsys, "import", str(VB_PDFS[1]), "--no-ai")[0] == 0
    tx, phrase = _merchant_tx(VB_PDFS[1])
    r = json.loads(run(kalan, capsys, "kategori", "--add", phrase, "insurance", "--no-apply", "--json")[1])
    assert r["applied"] == 0
    stored = kalan.statement(kalan.statements()[0].id)
    assert next(t for t in stored.transactions if t.id == tx.id).category != "insurance"


def test_category_names(kalan, capsys):
    code, out, _ = run(kalan, capsys, "--lang", "tr", "kategori", "--kategoriler")
    assert code == 0
    assert any(line.split()[:2] == ["groceries", "market"] for line in out.splitlines())


# -- teshis -------------------------------------------------------------------------------------------------


def test_diagnose_prints_the_issue_text(kalan, capsys):
    code, out, err = run(kalan, capsys, "teshis", str(VB_PDFS[1]))
    assert code == 0
    assert out == kalan.diagnose(VB_PDFS[1])["text"]
    assert out.startswith("kalan ") and "--- shapes" in out
    assert "no text and no amounts" in err  # the note goes to stderr, so the report can be piped as is
    report = json.loads(run(kalan, capsys, "diagnose", str(VB_PDFS[1]), "--json")[1])
    assert report["page_count"] >= 1 and "shapes" in report
    assert kalan.statements() == []  # nothing stored


def test_diagnose_encrypted(kalan, capsys, locked):
    assert error_code(kalan, capsys, "teshis", str(locked)) == "pdf_password_required"
    assert error_code(kalan, capsys, "teshis", str(locked), "--password", "yanlis") == "pdf_password_wrong"
    code, out, _ = run(kalan, capsys, "teshis", str(locked), "--password", PASSWORD)
    assert code == 0 and out == kalan.diagnose(VB_PDFS[0])["text"]


def test_diagnose_rejects_other_files(kalan, capsys, tmp_path):
    other = tmp_path / "x.pdf"
    other.write_text("ORNEK", encoding="utf-8")
    assert error_code(kalan, capsys, "teshis", str(other)) == "not_pdf"
    assert error_code(kalan, capsys, "teshis", str(tmp_path / "yok.pdf")) == "file_unreadable"
