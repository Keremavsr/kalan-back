from kalan.models import InstallmentInfo, PrintedSummary
from kalan.parsers.tr_vakifbank_worldcard import VakifbankWorldcard
from kalan.pipeline import parse_text
from kalan.reconcile import buckets, reconcile, remaining_installments
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


def test_fees_inside_period_and_remaining_installments():
    st = VakifbankWorldcard().parse(_text())
    b = buckets(st)
    plan = InstallmentInfo(remaining_count=2, remaining_amount_each=500, is_last=False, plan_id="pl_x")
    rows = [*st.transactions[:-1], st.transactions[-1].model_copy(update={"installment": plan})]
    st = st.model_copy(update={"transactions": rows})
    printed = PrintedSummary(
        previous_balance=b["carry_over"],
        period_transactions=b["period"] + b["interest_and_fees"],
        interest_and_fees=None,
        payments=b["payments"],
        statement_total=st.statement_total,
        remaining_installments=remaining_installments(st),
    )
    r = reconcile(st.model_copy(update={"printed_summary": printed}))
    names = [c.name for c in r.checks]
    assert "interest_and_fees" not in names and "remaining_installments" in names
    assert r.status == "ok", r.checks
    assert r.expected == b["period"] + b["interest_and_fees"]
    assert remaining_installments(st) >= 1000
    bad = printed.model_copy(update={"remaining_installments": 1})
    checks = {c.name: c.status for c in reconcile(st.model_copy(update={"printed_summary": bad})).checks}
    assert checks["remaining_installments"] == "mismatch"


def test_pipeline_attaches_reconciliation():
    st = parse_text(_text())
    assert st.reconciliation is not None and st.reconciliation.status == "ok"
