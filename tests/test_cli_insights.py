"""CLI commands for the insights, in Turkish and English, on the synthetic Garanti fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalan.cli import main
from kalan.commands import Money, percent, rate
from kalan.config import Config
from kalan.i18n import MESSAGES, resolve_lang, set_lang
from kalan.imports import parse_import
from kalan.models import FixedCost
from kalan.service import Kalan
from kalan.store import Store

GB_PDFS = sorted((Path(__file__).parent / "fixtures" / "garanti").glob("gb-*.pdf"))
TURKISH = set("çğıöşüÇĞİÖŞÜ")


@pytest.fixture(scope="module")
def loaded_dir(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("insights")
    config = Config(data_dir=folder)
    k = Kalan(config=config, store=Store(config.db_path))
    for pdf in GB_PDFS:
        k.import_file(pdf, ai_fallback=False)
    k.store.add_fixed_cost(FixedCost(id="fc_ornek1", name="ORNEK KIRA", amount=999999, currency="TRY", day=5,
                                     start="2026-01", end=None, category=None))
    return folder


@pytest.fixture
def kalan(loaded_dir) -> Kalan:
    config = Config(data_dir=loaded_dir)
    return Kalan(config=config, store=Store(config.db_path))


def run(kalan: Kalan, capsys, lang: str, *argv: str) -> tuple[int, str, str]:
    code = main(["--lang", lang, *argv], kalan=kalan)
    out, err = capsys.readouterr()
    return code, out, err


def run_json(kalan: Kalan, capsys, *argv: str):
    code, out, err = run(kalan, capsys, "tr", *argv, "--json")
    assert code == 0, err
    return json.loads(out)


def test_language_choice(monkeypatch):
    monkeypatch.setenv("KALAN_LANG", "tr")
    assert resolve_lang() == "tr" and resolve_lang("en") == "en"
    monkeypatch.setenv("KALAN_LANG", "en")
    assert resolve_lang() == "en"
    monkeypatch.delenv("KALAN_LANG")
    monkeypatch.setenv("LC_ALL", "tr_TR.UTF-8")
    assert resolve_lang() == "tr"
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert resolve_lang() == "en"


def test_turkish_formats():
    set_lang("tr")
    try:
        assert Money("tr")(123456, "TRY") == "1.234,56 TL" and Money("tr")(123456, "USD") == "1.234,56 USD"
        assert percent("0.25") == "%25,0" and percent("-0.1") == "-%10,0" and rate("3.11") == "%3,11"
    finally:
        set_lang("en")
    assert percent("0.25") == "25.0%" and rate("3.11") == "3.11%"


def test_catalogue_has_both_languages():
    for key, (en, tr) in MESSAGES.items():
        assert en or tr, key
        if key.startswith(("col.", "help.")):
            assert tr, key


def test_help_in_turkish_and_english(kalan, capsys):
    with pytest.raises(SystemExit):
        main(["--lang", "tr", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("kullanım: kalan") and "seçenekler:" in out and "yalnız asgari ödenirse" in out
    with pytest.raises(SystemExit):
        main(["--lang", "en", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: kalan") and "what paying only the minimum costs" in out


def test_lang_after_the_command_and_kalan_lang(kalan, capsys, monkeypatch):
    assert main(["takvim", "--lang", "tr", "-n", "2"], kalan=kalan) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split()[:3] == ["Ay", "Taksitler", "Plan"] and "Tutarlar TL cinsinden." in out
    monkeypatch.setenv("KALAN_LANG", "en")
    assert main(["takvim", "-n", "2"], kalan=kalan) == 0
    assert capsys.readouterr().out.rstrip().endswith("Amounts in TRY.")


def test_errors_in_turkish(kalan, capsys):
    code, _, err = run(kalan, capsys, "tr", "erkenkapama", "pl_yok")
    assert code == 1 and err.strip() == "kalan: Bu kimlikte açık taksit planı yok. (plan_not_found)"
    code, _, err = run(kalan, capsys, "en", "erkenkapama", "pl_yok")
    assert code == 1 and err.strip() == "kalan: No open installment plan with this id. (plan_not_found)"


def test_minimum_payment(kalan, capsys):
    data = run_json(kalan, capsys, "asgari")
    assert data == kalan.minimum_payment()
    code, out, _ = run(kalan, capsys, "tr", "asgari", "--cycles", "2")
    assert code == 0 and "Yalnız asgari ödenirse" in out and " TL" in out and "Varsayımlar:" in out
    table_rows = [line for line in out.splitlines() if line[:2].strip() in {"1", "2"}]
    assert len(table_rows) == 2
    code, out, _ = run(kalan, capsys, "en", "minimum", "--cycles", "0", "--with-installments")
    assert code == 0 and "Paying only the minimum" in out and "Assumptions:" in out
    code, _, err = run(kalan, capsys, "tr", "asgari", "--statement", "st_yok")
    assert code == 1 and "statement_not_found" in err


def test_card_fees_and_prompt(kalan, capsys):
    items = run_json(kalan, capsys, "aidat")["items"]
    assert items == kalan.card_fees() and items
    code, out, _ = run(kalan, capsys, "tr", "aidat")
    assert code == 0 and "İade" in out and items[0]["transaction_id"] in out and "hayır" in out
    code, out, _ = run(kalan, capsys, "tr", "aidat", "--prompt", items[0]["transaction_id"])
    prompt = kalan.card_fee_prompt(items[0]["transaction_id"])["prompt"]
    assert code == 0 and "Banka reddederse başvuru yeri" in out and prompt.splitlines()[0] in out
    code, out, _ = run(kalan, capsys, "en", "fees", "--prompt", items[0]["transaction_id"])
    assert code == 0 and "If the bank refuses, apply to" in out
    code, _, err = run(kalan, capsys, "tr", "aidat", "--prompt", "tx_yok")
    assert code == 1 and "card_fee_not_found" in err


def test_which_card(kalan, capsys):
    data = run_json(kalan, capsys, "hangikart", "--date", "2026-11-02")
    assert data["date"] == "2026-11-02" and data["cards"]
    code, out, _ = run(kalan, capsys, "tr", "hangikart", "--date", "2026-11-02")
    assert code == 0 and "en uygun kart" in out and "gün faizsiz" in out and "Faizsiz gün" in out
    code, out, _ = run(kalan, capsys, "en", "which-card", "--date", "2026-11-02")
    assert code == 0 and "Best card for an installment purchase on 2026-11-02" in out
    code, _, err = run(kalan, capsys, "tr", "hangikart", "--date", "02.11.2026")
    assert code == 1 and "invalid_date" in err


def test_share_card(kalan, capsys, tmp_path):
    svg = tmp_path / "kart.svg"
    code, out, _ = run(kalan, capsys, "tr", "paylas", "--svg", str(svg))
    assert code == 0 and out.startswith("Önümüzdeki 12 ayda") and "SVG kart kaydedildi" in out
    text = svg.read_text(encoding="utf-8")
    assert text.startswith("<svg") and "TL" not in text  # amounts are off by default
    code, out, _ = run(kalan, capsys, "en", "share", "--amounts")
    assert code == 0 and out.startswith("Over the next 12 months")
    data = run_json(kalan, capsys, "paylas")
    assert data["show_amounts"] is False and data["locale"] == "tr"


def test_forecast(kalan, capsys):
    data = run_json(kalan, capsys, "tahmin")
    assert data == kalan.forecast()
    code, out, _ = run(kalan, capsys, "tr", "tahmin", "--no-fixed")
    assert code == 0 and "ekstresi tahmini" in out and "toplama dahil değil" in out and "Açık dönem" in out
    code, out, _ = run(kalan, capsys, "en", "forecast", "--backtest")
    assert code == 0 and ("Accuracy over" in out or "Not enough history" in out)
    assert run_json(kalan, capsys, "tahmin", "--backtest") == kalan.forecast_backtest()


def test_early_payoff(kalan, capsys):
    data = run_json(kalan, capsys, "erkenkapama")
    assert data == kalan.early_payoff() and data["plans"]
    plan_id = data["plans"][0]["plan_id"]
    code, out, _ = run(kalan, capsys, "tr", "erkenkapama")
    assert code == 0 and plan_id in out and "Kalan tutar" in out
    code, out, _ = run(kalan, capsys, "tr", "erkenkapama", plan_id)
    assert code == 0 and "taksit" in out and "erken kapama kuralını bilmiyor" in out
    code, out, _ = run(kalan, capsys, "en", "payoff", plan_id)
    assert code == 0 and "instead of" in out and "With payoff" in out
    code, _, err = run(kalan, capsys, "tr", "erkenkapama", plan_id, "--month", "2199-01")
    assert code == 1 and "month_outside_schedule" in err


def test_fixed_cost_matches(kalan, capsys):
    data = run_json(kalan, capsys, "sabitler")
    assert data == kalan.fixed_cost_matches() and data["costs"][0]["name"] == "ORNEK KIRA"
    code, out, _ = run(kalan, capsys, "tr", "sabitler")
    assert code == 0 and "ORNEK KIRA" in out and "Eksik aylar" in out and "gün içindeyse eşleşir" in out
    code, out, _ = run(kalan, capsys, "en", "fixed")
    assert code == 0 and "Missing months" in out


@pytest.mark.parametrize("fmt", ["hledger", "beancount", "ofx", "csv"])
def test_export_to_stdout_matches_service(kalan, capsys, fmt):
    code, out, _ = run(kalan, capsys, "tr", "disari", "--format", fmt)
    assert code == 0 and out == kalan.export(fmt)[0]


def test_export_to_file_round_trips(kalan, capsys, tmp_path):
    path = tmp_path / "ornek.csv"
    code, out, _ = run(kalan, capsys, "tr", "disari", "--format", "csv", "--out", str(path))
    assert code == 0 and out.strip() == f"{path} yazıldı (csv, işlemler)."
    rows = parse_import(path.read_bytes(), path.name, None, "TRY").transactions
    assert len(rows) == len(kalan.export("csv")[0].splitlines()) - 1
    code, out, _ = run(kalan, capsys, "en", "export", "--format", "csv", "--what", "plans", "--out", str(path))
    assert code == 0 and "plans" in out and path.read_text(encoding="utf-8").startswith("plan_id,")
    code, _, err = run(kalan, capsys, "tr", "disari", "--format", "ofx", "--what", "plans")
    assert code == 1 and "yalnız CSV" in err
    data = run_json(kalan, capsys, "disari", "--format", "hledger")
    assert data["format"] == "hledger" and data["text"] == kalan.export("hledger")[0]


def test_existing_commands_speak_turkish(kalan, capsys):
    for argv, expected in [(["statements"], "Son ödeme"), (["ay", "2026-02"], "harcama"),
                           (["abonelikler"], "İşyeri"), (["uyarilar"], "Uyarı"),
                           (["islemler", "--limit", "3"], "Açıklama"),
                           (["simule", "1000", "3", "--start", "2099-01"], "taksit")]:
        code, out, err = run(kalan, capsys, "tr", *argv)
        assert code == 0, err
        assert expected in out, argv
        assert TURKISH & set(out), argv
