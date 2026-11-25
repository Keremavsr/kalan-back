from kalan.parsers.tr_vakifbank_worldcard import VakifbankWorldcard
from kalan.pipeline import parse_text
from kalan.reconcile import buckets, reconcile
from tests.test_vakifbank_text import _text


def test_ok_statement():
    st = VakifbankWorldcard().parse(_text())
    b = buckets(st)
    assert b == {"carry_over": 100000, "period": 299050, "interest_and_fees": 7000, "payments": 100000}
    r = reconcile(st)
    assert r.status == "ok"
    assert (r.expected, r.computed, r.difference) == (299050, 299050, 0)
    assert {c.name for c in r.checks} >= {"period_transactions", "interest_and_fees", "payments", "statement_total"}


def test_mismatch_is_a_warning_with_numbers():
    st = VakifbankWorldcard().parse(_text(period="2,900.00"))
    r = reconcile(st)
    assert r.status == "mismatch"
    assert (r.expected, r.computed, r.difference) == (290000, 299050, 9050)
    by_name = {c.name: c for c in r.checks}
    assert by_name["printed_summary_arithmetic"].status == "mismatch"
    assert by_name["interest_and_fees"].status == "ok"


def test_total_mismatch_detected():
    st = VakifbankWorldcard().parse(_text(total="2,000.00"))
    r = reconcile(st)
    assert r.status == "mismatch"
    assert {c.name: c.difference for c in r.checks}["statement_total"] == 106050


def test_pipeline_attaches_reconciliation():
    st = parse_text(_text())
    assert st.reconciliation is not None and st.reconciliation.status == "ok"
