from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from kalan.pipeline import parse_pdf
from kalan.store import DuplicateStatementError, Settings, Store, migrations
from tests.conftest import VB_PDFS


@pytest.fixture(scope="module")
def parsed():
    return parse_pdf(VB_PDFS[4])


def test_migrations_are_numbered_and_applied(store):
    found = migrations()
    assert [n for n, _, _ in found] == list(range(1, len(found) + 1))
    assert store.schema_version() == found[-1][0]
    assert Store(store.path).schema_version() == found[-1][0]  # re-opening applies nothing twice


def test_roundtrip_and_persistence(store, parsed):
    saved = store.add_statement(parsed)
    assert saved == parsed.model_copy(update={"analysis_status": "none"})
    again = Store(store.path).get_statement(parsed.id)
    assert again == saved


def test_duplicate_by_period_even_with_another_id(store, parsed):
    store.add_statement(parsed)
    other = parsed.model_copy(update={"id": "st_other", "transactions": []})
    with pytest.raises(DuplicateStatementError) as exc:
        store.add_statement(other)
    assert exc.value.existing_id == parsed.id


def test_duplicate_without_card_digits(store, parsed):
    a = parsed.model_copy(update={"id": "st_a", "card_last4": None, "transactions": []})
    store.add_statement(a)
    with pytest.raises(DuplicateStatementError):
        store.add_statement(a.model_copy(update={"id": "st_b"}))
    # The unique index enforces it too.
    with pytest.raises(sqlite3.IntegrityError), sqlite3.connect(store.path) as con:
        con.execute(
            "INSERT INTO statements (id, bank, card_last4, period_start, period_end, data, created_at) "
            "VALUES ('st_c', ?, NULL, ?, ?, '{}', 'now')",
            (a.bank, a.period_start.isoformat(), a.period_end.isoformat()),
        )


def test_delete_cascades(store, parsed):
    store.add_statement(parsed)
    assert store.delete_statement(parsed.id)
    with sqlite3.connect(store.path) as con:
        assert con.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0
    assert not store.delete_statement(parsed.id)


def test_settings_and_provider(store):
    assert store.get_settings() == Settings()
    store.put_settings(Settings(locale="en", base_currency="EUR", months_ahead=24))
    assert Store(store.path).get_settings().base_currency == "EUR"
    assert store.selected_provider() is None
    store.set_selected_provider("codex-cli")
    assert store.selected_provider() == "codex-cli"


def test_analysis_applies_categories_only_to_its_statement(store, parsed):
    store.add_statement(parsed)
    ids = [t.id for t in parsed.transactions]
    ok = store.save_analysis(parsed.id, "fake", {ids[0]: "fees", "tx_elsewhere": "cash"}, {ids[1]},
                             {"locale": "tr", "summary": "s", "watch_items": []})
    assert ok
    st = store.get_statement(parsed.id)
    assert st.analysis_status == "done"
    assert st.transactions[0].category == "fees"
    assert store.model_recurring_ids() == {ids[1]}
    assert store.get_analysis(parsed.id)["provider"] == "fake"
    assert not store.save_analysis("st_missing", "fake", {}, set(), {})


def test_jobs(store, parsed):
    store.add_statement(parsed)
    job = store.create_job("job_1", parsed.id, "fake")
    assert job.status == "pending" and store.get_statement(parsed.id).analysis_status == "pending"
    assert store.pending_job_for(parsed.id).id == "job_1"
    store.finish_job("job_1", "failed", "nope")
    assert store.get_job("job_1").error == "nope"
    assert store.get_statement(parsed.id).analysis_status == "failed"
    assert store.pending_job_for(parsed.id) is None


def test_data_dir_override(monkeypatch, tmp_path):
    from kalan.config import Config

    monkeypatch.setenv("KALAN_DATA_DIR", str(tmp_path / "x"))
    assert Config().db_path == tmp_path / "x" / "kalan.db"
    monkeypatch.delenv("KALAN_DATA_DIR")
    assert Config().db_path.name == "kalan.db" and "kalan" in str(Config().data_dir).lower()


def test_manual_plan_is_removed_with_its_transaction(store):
    from kalan.models import ManualInstallment, Transaction

    tx = Transaction(id="tx_m1", date=date(2026, 9, 1), description="X", amount=600, direction="debit",
                     kind="purchase", source="manual")
    plan = ManualInstallment(id="pl_m1", description="X", start_date=date(2026, 9, 1), total_count=3, amount_each=200)
    store.add_manual(tx, plan)
    assert [p.id for p in store.manual_plans()] == ["pl_m1"]
    assert store.delete_manual_transaction("tx_m1") == "deleted"
    assert store.manual_plans() == []
