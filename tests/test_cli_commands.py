"""Data commands of the CLI on synthetic fixtures: tables by default, ``--json`` for scripts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalan.ai.adapters import FakeAdapter, ProviderRegistry
from kalan.cli import main
from kalan.commands import Money, load, table
from kalan.commands.plans import parse_amount_arg
from kalan.config import Config
from kalan.service import Kalan, ServiceError
from kalan.store import Store
from tests.test_api import answer_for

GB = Path(__file__).parent / "fixtures" / "garanti"
GB_PDFS = sorted(GB.glob("gb-*.pdf"))

TR_CSV = """Tarih;Açıklama;Tutar
05.02.2099;ORNEK MARKET;1.234,56
10.02.2099;ORNEK MOBILYA 3/10 TAKSIT;300,00
"""


@pytest.fixture(autouse=True)
def _english(monkeypatch):
    """English output unless a test passes --lang; the OS locale of the test machine must not matter."""
    monkeypatch.setenv("KALAN_LANG", "en")


@pytest.fixture
def kalan(tmp_path) -> Kalan:
    config = Config(data_dir=tmp_path)
    registry = ProviderRegistry([FakeAdapter(answer_for)], ttl=0)
    return Kalan(config=config, store=Store(config.db_path), registry=registry, timeout=5)


def run(kalan: Kalan, capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv), kalan=kalan)
    out, err = capsys.readouterr()
    return code, out, err


def run_json(kalan: Kalan, capsys, *argv: str):
    code, out, err = run(kalan, capsys, *argv, "--json")
    assert code == 0, err
    return json.loads(out)


@pytest.fixture
def loaded(kalan, capsys) -> Kalan:
    for pdf in GB_PDFS[:4]:
        assert run(kalan, capsys, "import", str(pdf), "--no-ai")[0] == 0
    return kalan


def test_every_command_has_a_turkish_name_and_an_english_alias():
    names = {c.name: c.aliases for c in load().values()}
    for name, alias in [("import", None), ("statements", None), ("takvim", "schedule"), ("ay", "month"),
                        ("uyarilar", "alerts"), ("abonelikler", "subscriptions"), ("simule", "simulate"),
                        ("analiz", "analyze")]:
        assert name in names
        if alias:
            assert alias in names[name]


def test_import_pdf_table_and_json(kalan, capsys):
    code, out, _ = run(kalan, capsys, "import", str(GB_PDFS[0]), "--no-ai")
    assert code == 0 and out.startswith("Stored statement st_") and "totals check: ok" in out
    data = run_json(kalan, capsys, "import", str(GB_PDFS[1]), "--no-ai")
    assert data["kind"] == "statement" and data["statement"]["bank"] == "garanti"
    assert isinstance(data["statement"]["statement_total"], int)


def test_import_duplicate_is_an_error_with_code(kalan, capsys):
    run(kalan, capsys, "import", str(GB_PDFS[0]), "--no-ai")
    code, out, err = run(kalan, capsys, "import", str(GB_PDFS[0]), "--no-ai", "--json")
    assert code == 1 and "(duplicate)" in err
    assert json.loads(out)["error"]["code"] == "duplicate"


def test_import_forced_bank(kalan, capsys):
    data = run_json(kalan, capsys, "import", str(GB_PDFS[0]), "--bank", "tr_garanti_bonus")
    assert data["statement"]["parser_id"] == "tr_garanti_bonus"
    code, _, err = run(kalan, capsys, "import", str(GB_PDFS[1]), "--bank", "tr_vakifbank_worldcard", "--no-ai")
    assert code == 1 and "unknown_format" in err
    code, _, err = run(kalan, capsys, "import", str(GB_PDFS[1]), "--bank", "nope")
    assert code == 1 and "unknown_bank" in err


def test_import_csv(kalan, capsys, tmp_path):
    path = tmp_path / "ornek.csv"
    path.write_text(TR_CSV, encoding="utf-8")
    code, out, _ = run(kalan, capsys, "import", str(path))
    assert code == 0 and "Imported 2 of 2 rows (csv, TRY)" in out
    data = run_json(kalan, capsys, "import", str(path))
    assert data["kind"] == "import" and data["skipped_duplicates"] == 2


def test_import_missing_file(kalan, capsys, tmp_path):
    code, _, err = run(kalan, capsys, "import", str(tmp_path / "yok.pdf"))
    assert code == 1 and "file_unreadable" in err


def test_statements(loaded, capsys):
    code, out, _ = run(loaded, capsys, "statements")
    lines = out.splitlines()
    assert code == 0 and lines[0].split()[:3] == ["Id", "Card", "Last4"]
    assert len(lines) == 2 + 4
    data = run_json(loaded, capsys, "ekstreler")
    ends = [s["period_end"] for s in data["items"]]
    assert ends == sorted(ends, reverse=True)


def test_statements_empty(kalan, capsys):
    assert run(kalan, capsys, "statements")[1].strip() == "No statements yet."


def test_schedule_matches_service(loaded, capsys):
    data = run_json(loaded, capsys, "takvim", "-n", "4")
    assert data == loaded.schedule(4).model_dump(mode="json")
    assert data == run_json(loaded, capsys, "schedule", "--months", "4")
    code, out, _ = run(loaded, capsys, "takvim", "-n", "4")
    assert code == 0 and out.splitlines()[0].split() == ["Month", "Installments", "Plans"]
    assert "ORNEK ELEKTRONIK AVM" in out and out.rstrip().endswith("Amounts in TRY.")
    assert run(loaded, capsys, "takvim", "-n", "99")[0] == 1


def test_month_summary(loaded, capsys):
    data = run_json(loaded, capsys, "ay", "2026-02")
    assert data == loaded.month("2026-02")
    code, out, _ = run(loaded, capsys, "month", "2026-02")
    assert code == 0 and out.startswith("2026-02: spent ")
    code, _, err = run(loaded, capsys, "ay", "2026-13")
    assert code == 1 and "invalid_month" in err


def test_alerts_and_subscriptions(loaded, capsys):
    alerts = run_json(loaded, capsys, "uyarilar")["items"]
    assert alerts == [a.model_dump(mode="json") for a in loaded.alerts()]
    code, out, _ = run(loaded, capsys, "alerts")
    assert code == 0 and out.splitlines()[0].split() == ["Severity", "Month", "Alert"]
    assert len(out.splitlines()) == 2 + len(alerts)
    subs = run_json(loaded, capsys, "abonelikler")["items"]
    assert subs == loaded.subscriptions() and subs
    out = run(loaded, capsys, "subscriptions")[1]
    assert all(s["merchant"] in out for s in subs)


def test_empty_alerts_and_subscriptions(kalan, capsys):
    assert run(kalan, capsys, "uyarilar")[1].strip() == "No alerts."
    assert run(kalan, capsys, "abonelikler")[1].strip() == "No recurring charges found."


def test_simulate(loaded, capsys):
    data = run_json(loaded, capsys, "simule", "9.999,99", "6", "--start", "2099-01")
    assert data["purchase"]["amount"] == 999999 and data["purchase"]["count"] == 6
    assert [i["amount"] for i in data["purchase"]["installments"]] == [166669] + [166666] * 5
    code, out, _ = run(loaded, capsys, "simulate", "1000", "3", "--interest", "3", "--start", "2099-01")
    assert code == 0 and "total 1,060.59 TRY (interest 60.59 TRY)" in out
    code, _, err = run(loaded, capsys, "simule", "abc", "3")
    assert code == 1 and "invalid_amount" in err
    code, _, err = run(loaded, capsys, "simule", "100", "0")
    assert code == 1 and "invalid_simulation" in err


@pytest.mark.parametrize(("text", "minor"), [
    ("9.999,99", 999999), ("9999,99", 999999), ("9,999.99", 999999), ("9999.99", 999999),
    ("1.234", 123400), ("1,5", 150), ("100", 10000),
])
def test_amount_argument(text, minor):
    assert parse_amount_arg(text, "TRY") == minor


def test_transactions_search(loaded, capsys):
    items = run_json(loaded, capsys, "islemler", "-s", "ornekmarket", "--limit", "0")["items"]
    assert items and all("ORNEKMARKET" in t["description"] for t in items)
    dates = [t["date"] for t in items]
    assert dates == sorted(dates, reverse=True)
    month = items[0]["month"]
    assert all(t["month"] == month for t in run_json(loaded, capsys, "transactions", "--month", month)["items"])
    assert run(loaded, capsys, "islemler", "-s", "hic-yok-boyle-bir-sey")[1].strip() == "No transactions match."


def test_analyze_with_selected_provider(loaded, capsys):
    st_id = loaded.statements()[0].id
    code, _, err = run(loaded, capsys, "analiz", st_id)
    assert code == 1 and "no_provider" in err
    loaded.store.set_selected_provider("fake")
    code, out, _ = run(loaded, capsys, "analiz", st_id)
    assert code == 0 and "Kısa bir özet." in out and "* Faiz" in out
    data = run_json(loaded, capsys, "analyze", st_id, "--cached")
    assert data["provider"] == "fake" and data["commentary"]["summary"] == "Kısa bir özet."
    code, _, err = run(loaded, capsys, "analiz", "st_missing")
    assert code == 1 and "statement_not_found" in err


def test_data_dir_option(tmp_path, capsys, monkeypatch):
    assert main(["--data-dir", str(tmp_path), "import", str(GB_PDFS[0]), "--no-ai"]) == 0
    assert (tmp_path / "kalan.db").is_file()
    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path), "statements", "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["items"]) == 1


def test_table_and_money_helpers():
    assert table(["A", "Bee"], [["x", 1], ["long", 22]], right=[1]) == ["A     Bee", "----  ---", "x       1",
                                                                         "long   22"]
    assert table(["A"], []) == []
    assert Money("tr")(123456, "TRY") == "1.234,56 TL"
    assert Money("en")(123456, "USD") == "1,234.56 USD"
    assert Money("tr")(1234, "JPY") == "1.234 JPY"
    assert Money("tr")(None) == "-"


def test_service_error_shape():
    err = ServiceError("x", "message", {"a": 1})
    assert err.as_dict() == {"error": {"code": "x", "message": "message", "details": {"a": 1}}}
