"""Check parsed transactions against the totals the bank printed.

The bank's ``HESAP ÖZETİ`` splits a period into four buckets. Parsed rows are put in the same buckets
by kind (checked against real VakıfBank statements):

* ``carry_over``: the opening balance row, compared with ``Önceki Hesap Bakiyesi``;
* ``payment``: compared with ``Ödemeler``;
* ``fee`` and ``interest`` (card fee, BSMV, KKDF, vade farkı, interest): ``Toplam Faiz ve Ücretler``;
* everything else, debits minus credits (purchases, installments, cash advance principal, tax
  payments, refunds, early-payoff reversals): ``Dönem İçi İşlemler``.

Finally ``previous + period + interest/fees - payments`` must equal ``Dönem Borcunuz``. The headline
numbers (``expected``/``computed``/``difference``) are those of the period check, as in ``docs/api.md``.
A mismatch is reported, never raised.

Banks that print no separate fee line (``interest_and_fees`` is ``None``, Yapı Kredi) count interest and
fees inside the period total, so the period check compares the period and fee buckets together. When the
bank prints the installments still to come (``remaining_installments``), the remaining count times the
amount each of every installment row must add up to it.
"""

from __future__ import annotations

from kalan.models import Reconciliation, ReconciliationCheck, Statement

FEE_KINDS = frozenset({"fee", "interest"})


def buckets(statement: Statement) -> dict[str, int]:
    out = {"carry_over": 0, "period": 0, "interest_and_fees": 0, "payments": 0}
    for tx in statement.transactions:
        if tx.source != "statement":
            continue
        if tx.kind == "carry_over":
            out["carry_over"] += tx.signed_amount
        elif tx.kind == "payment":
            out["payments"] -= tx.signed_amount
        elif tx.kind in FEE_KINDS:
            out["interest_and_fees"] += tx.signed_amount
        else:
            out["period"] += tx.signed_amount
    return out


def remaining_installments(statement: Statement) -> int:
    """Installments still to come after this statement, from the statement's own installment rows."""
    return sum(
        t.installment.remaining_count * t.installment.remaining_amount_each
        for t in statement.transactions
        if t.source == "statement" and t.installment is not None
    )


def _check(name: str, expected: int, computed: int) -> ReconciliationCheck:
    diff = computed - expected
    return ReconciliationCheck(
        name=name, status="ok" if diff == 0 else "mismatch", expected=expected, computed=computed, difference=diff
    )


def reconcile(statement: Statement) -> Reconciliation:
    b = buckets(statement)
    balance = b["carry_over"] + b["period"] + b["interest_and_fees"] - b["payments"]
    checks: list[ReconciliationCheck] = []
    p = statement.printed_summary
    if p is not None:
        if p.interest_and_fees is None:
            checks.append(_check("period_transactions", p.period_transactions, b["period"] + b["interest_and_fees"]))
        else:
            checks.append(_check("period_transactions", p.period_transactions, b["period"]))
            checks.append(_check("interest_and_fees", p.interest_and_fees, b["interest_and_fees"]))
        checks.append(_check("payments", p.payments, b["payments"]))
        checks.append(_check("previous_balance", p.previous_balance, b["carry_over"]))
        printed_balance = p.previous_balance + p.period_transactions + (p.interest_and_fees or 0) - p.payments
        checks.append(_check("printed_summary_arithmetic", p.statement_total, printed_balance))
        if p.remaining_installments is not None:
            checks.append(_check("remaining_installments", p.remaining_installments, remaining_installments(statement)))
    checks.append(_check("statement_total", statement.statement_total, balance))
    head = checks[0]
    status = "ok" if all(c.status == "ok" for c in checks) else "mismatch"
    return Reconciliation(
        status=status, expected=head.expected, computed=head.computed, difference=head.difference, checks=checks
    )
