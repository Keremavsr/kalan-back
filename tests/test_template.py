"""Declarative bank templates: schema, engine on an invented second layout, user folder, CLI check."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from kalan import parsers
from kalan.cli import main
from kalan.parsers.template import (
    BUILTIN_DIR,
    SCHEMA_FILE,
    Template,
    TemplateParser,
    load_template,
    template_json_schema,
)
from kalan.pipeline import parse_text
from kalan.template_check import check

HERE = Path(__file__).parent / "fixtures" / "templates"
TPL = HERE / "br_bancoexemplo_ouro.yaml"
TXT = HERE / "br_bancoexemplo_ouro.txt"
BUILTIN_IDS = ["tr_vakifbank_worldcard", "tr_yapikredi_world"]
VB_PDF = sorted((Path(__file__).parent / "fixtures" / "vakifbank").glob("vb-*.pdf"))[6]


def _parser() -> TemplateParser:
    return TemplateParser(load_template(TPL))


def _text() -> str:
    return TXT.read_text(encoding="utf-8")


def _raw() -> dict:
    return yaml.safe_load(TPL.read_text(encoding="utf-8"))


@pytest.fixture
def user_dir(monkeypatch, tmp_path):
    folder = tmp_path / "templates"
    folder.mkdir()
    monkeypatch.setenv("KALAN_DATA_DIR", str(tmp_path))
    yield folder
    parsers.reload()


def test_schema_file_is_current():
    assert json.loads(SCHEMA_FILE.read_text(encoding="utf-8")) == template_json_schema()


def test_builtin_templates_validate():
    files = sorted(BUILTIN_DIR.glob("*.yaml"))
    assert [f.stem for f in files] == BUILTIN_IDS
    for f in files:
        assert load_template(f).id == f.stem


def test_vakifbank_runs_from_its_template():
    from kalan.parsers.tr_vakifbank_worldcard import VakifbankWorldcard

    p = parsers.get_parser("tr_vakifbank_worldcard")
    assert isinstance(p, VakifbankWorldcard) and isinstance(p, TemplateParser)
    assert p.source == BUILTIN_DIR / "tr_vakifbank_worldcard.yaml"


def test_second_layout_header_and_rows():
    p = _parser()
    assert p.detect(_text())
    st = p.parse(_text())
    assert st.parser_id == "br_bancoexemplo_ouro" and st.currency == "BRL"
    assert (st.card_last4, st.statement_total, st.minimum_payment, st.credit_limit) == ("4321", 69456, 10418, 800000)
    assert st.period_end.isoformat() == "2026-03-01" and st.due_date.isoformat() == "2026-03-10"
    assert st.period_start.isoformat() == "2026-02-03"  # day after the carry-over row
    assert st.page_count == 2
    kinds = [t.kind for t in st.transactions]
    assert kinds == ["carry_over", "payment", "purchase", "installment", "installment", "purchase", "refund", "fee",
                     "tax"]
    assert [t.direction for t in st.transactions][:2] == ["debit", "credit"]
    moveis, livraria = st.transactions[3], st.transactions[4]
    assert moveis.description == "LOJA DE MOVEIS"
    i = moveis.installment
    assert (i.index, i.total_count, i.remaining_count, i.remaining_amount_each, i.is_last) == (3, 10, 7, 30000, False)
    j = livraria.installment
    assert (j.index, j.total_count, j.remaining_count, j.is_last) == (10, 10, 0, True)
    app = st.transactions[5]
    assert (app.original_amount, app.original_currency, app.amount) == (1200, "USD", 6000)
    assert app.merchant == "APP STREAMING"


def test_second_layout_reconciles_through_pipeline(user_dir):
    shutil.copy(TPL, user_dir / TPL.name)
    parsers.reload()
    st = parse_text(_text())
    assert st.parser_id == "br_bancoexemplo_ouro"
    assert st.reconciliation.status == "ok", st.reconciliation
    assert parsers.template_errors() == []


def test_plan_id_stable_across_months():
    p = _parser()
    nxt = (_text().replace("LOJA DE MOVEIS 03/10", "LOJA DE MOVEIS 04/10")
           .replace("Fechamento: 01/03/2026", "Fechamento: 01/04/2026"))
    a = p.parse(_text()).transactions[3].installment.plan_id
    b = p.parse(nxt).transactions[3].installment.plan_id
    assert a == b


def test_user_template_errors_are_skipped(user_dir):
    (user_dir / "broken.yaml").write_text("id: [unclosed", encoding="utf-8")
    dup = _raw() | {"id": "tr_vakifbank_worldcard"}
    (user_dir / "dup.yaml").write_text(yaml.safe_dump(dup, allow_unicode=True), encoding="utf-8")
    bad = _raw()
    del bad["header"]["fields"]["due_date"]
    (user_dir / "bad.yaml").write_text(yaml.safe_dump(bad, allow_unicode=True), encoding="utf-8")
    parsers.reload()
    errors = parsers.template_errors()
    assert len(errors) == 3
    assert any("already used" in e for e in errors) and any("due_date" in e for e in errors)
    assert [p.id for p in parsers.all_parsers()] == BUILTIN_IDS


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (lambda t: t["table"].update(row_pattern="(?P<date>x)"), "description"),
        (lambda t: t["installment"].update(stem_remove=["(unclosed"]), "regular expression"),
        (lambda t: t["installment"].update(description=["(\\d+)"]), "named group 'k'"),
        (lambda t: t["kinds"].append({"kind": "fee"}), "needs match"),
        (lambda t: t.update(id="Bad-Id"), "id"),
        (lambda t: t.update(currency="reais"), "currency"),
        (lambda t: t["amount"].update(preset=None), "preset"),
        (lambda t: t["summary"]["fields"].pop("payments"), "payments"),
        (lambda t: t.update(colour="red"), "colour"),
    ],
)
def test_invalid_templates(change, needle):
    raw = _raw()
    change(raw)
    with pytest.raises(ValidationError) as exc:
        Template.model_validate(raw)
    assert needle in str(exc.value)


def test_check_quiet_prints_no_values():
    lines, code = check(TPL, TXT, quiet=True)
    blob = "\n".join(lines)
    assert code == 0
    assert "reconciliation: ok" in blob and "  statement_total: found" in blob
    assert "rows: 9 (" in blob
    for value in ("694,56", "69456", "MERCADO", "4321"):
        assert value not in blob


def test_check_verbose_shows_values_but_no_descriptions():
    lines, code = check(TPL, TXT)
    blob = "\n".join(lines)
    assert code == 0 and "694,56" in blob and "expected 66456 computed 66456" in blob
    assert "MERCADO" not in blob and "4321" not in blob


def test_check_reports_mismatch_and_bad_template(tmp_path):
    txt = tmp_path / "s.txt"
    txt.write_text(_text().replace("ANUIDADE 30,00", "ANUIDADE 31,00"), encoding="utf-8")
    lines, code = check(TPL, txt, quiet=True)
    assert code == 1 and "  interest_and_fees: mismatch" in lines
    bad = tmp_path / "bad.yaml"
    bad.write_text("id: x\n", encoding="utf-8")
    lines, code = check(bad, txt)
    assert code == 2 and lines[0] == "template: invalid"


def test_cli_template_check_on_vakifbank_pdf(capsys):
    code = main(["template", "check", str(BUILTIN_DIR / "tr_vakifbank_worldcard.yaml"), str(VB_PDF), "--quiet"])
    out = capsys.readouterr().out
    assert code == 0 and "detect: yes" in out and "reconciliation: ok" in out


def test_cli_template_schema(capsys):
    assert main(["template", "schema"]) == 0
    assert json.loads(capsys.readouterr().out)["title"] == "Template"
