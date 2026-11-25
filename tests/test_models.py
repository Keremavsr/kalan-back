from datetime import date

import pytest
from pydantic import ValidationError

from kalan.models import InstallmentInfo, Statement, Transaction


def _tx(**kw):
    base = dict(id="tx_1", date=date(2026, 9, 12), description="X", amount=1000, direction="debit", kind="purchase")
    base.update(kw)
    return Transaction(**base)


def test_money_is_int_minor_units():
    tx = _tx(amount=234560)
    assert tx.amount == 234560
    assert tx.signed_amount == 234560
    assert _tx(direction="credit").signed_amount == -1000


def test_negative_amount_rejected():
    with pytest.raises(ValidationError):
        _tx(amount=-1)


def test_unknown_kind_rejected():
    with pytest.raises(ValidationError):
        _tx(kind="groceries")


def test_statement_json_matches_contract_keys():
    st = Statement(
        id="st_1", bank="vakifbank", product="Worldcard", card_last4="0000",
        period_start=date(2026, 8, 25), period_end=date(2026, 9, 24), due_date=date(2026, 10, 4),
        statement_total=0, minimum_payment=0,
        transactions=[_tx(installment=InstallmentInfo(remaining_count=2, remaining_amount_each=125040,
                                                      is_last=False, plan_id="pl_1"))],
    )
    data = st.model_dump(mode="json")
    for key in ("id", "bank", "product", "card_last4", "period_start", "period_end", "due_date", "currency",
                "statement_total", "minimum_payment", "reconciliation", "analysis_status", "transactions",
                "interest_rates"):
        assert key in data
    tx = data["transactions"][0]
    for key in ("id", "date", "description", "amount", "currency", "direction", "kind", "installment",
                "category", "merchant", "source"):
        assert key in tx
    assert set(tx["installment"]) >= {"remaining_count", "remaining_amount_each", "is_last", "plan_id"}
    assert data["period_end"] == "2026-09-24"
    assert st.cut_off_month == "2026-09"
