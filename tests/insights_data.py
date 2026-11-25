"""Invented statements for the insight tests. Every amount, date and merchant is made up."""

from __future__ import annotations

from datetime import date

from kalan.models import InstallmentInfo, Statement, Transaction


def tx(tx_id: str, day: date, amount: int, kind: str = "purchase", description: str = "ORNEK MARKET",
       direction: str = "debit", installment: InstallmentInfo | None = None, **extra) -> Transaction:
    return Transaction(id=tx_id, date=day, description=description, amount=amount, direction=direction,
                       kind=kind, installment=installment, **extra)


def statement(st_id: str = "st_a", *, period_end: date = date(2099, 1, 15), due: date | None = None,
              total: int = 100000, minimum: int = 20000, limit: int | None = 5000000, bank: str = "ornekbank",
              last4: str | None = "0000", transactions: list[Transaction] | None = None,
              rates: dict[str, str] | None = None, next_period_end: date | None = None) -> Statement:
    start = date(period_end.year - (period_end.month == 1), (period_end.month - 2) % 12 + 1, period_end.day)
    return Statement(
        id=st_id, bank=bank, product="Ornek Kart", card_last4=last4, period_start=start, period_end=period_end,
        due_date=due or date.fromordinal(period_end.toordinal() + 10), statement_total=total,
        minimum_payment=minimum, credit_limit=limit, transactions=transactions or [], interest_rates=rates or {},
        next_period_end=next_period_end,
    )
