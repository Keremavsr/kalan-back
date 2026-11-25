"""Data models shared by parsers, calculations and (later) the HTTP API.

Shapes follow ``docs/api.md``. All money values are integers in minor units (kuruş, centavos) with
an ISO 4217 currency code next to them. Fields that the API contract does not list (for example the
printed statement summary) are extra information the parser found; they never replace a contract field.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Direction = Literal["debit", "credit"]
TransactionKind = Literal[
    "purchase",
    "installment",
    "cash_advance",
    "fee",
    "tax",
    "interest",
    "payment",
    "refund",
    "carry_over",
    "other",
]
Source = Literal["statement", "manual", "import"]
ReconciliationStatus = Literal["ok", "mismatch"]
AnalysisStatus = Literal["none", "pending", "done", "failed"]
Locale = Literal["tr", "en", "pt", "es", "he"]
"""Languages of the model commentary: Turkish, English, Portuguese, Spanish, Hebrew."""
ComponentRole = Literal["principal", "bsmv", "kkdf", "interest", "early_payoff"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallmentInfo(_Model):
    """Installment details of one statement row.

    ``remaining_count`` counts the installments still to come *after* this row. ``is_last`` is true
    when this row is the final installment (``Son Taksit``) or when an early payoff closed the plan.
    """

    remaining_count: int = Field(ge=0)
    remaining_amount_each: int = Field(ge=0)
    is_last: bool
    plan_id: str
    index: int | None = Field(default=None, ge=1, description="Installment number of this row, if printed")
    total_count: int | None = Field(default=None, ge=1, description="Plan length, if it can be derived")
    component: ComponentRole | None = Field(
        default=None, description="Role of the row inside an installment cash advance"
    )


class Transaction(_Model):
    id: str
    date: date
    description: str
    amount: int = Field(ge=0, description="Absolute value in minor units; sign is in direction")
    currency: str = "TRY"
    direction: Direction
    kind: TransactionKind
    installment: InstallmentInfo | None = None
    category: str | None = None
    merchant: str | None = None
    source: Source = "statement"
    original_amount: int | None = Field(default=None, description="Foreign currency amount, minor units")
    original_currency: str | None = None
    points: str | None = Field(default=None, description="Loyalty points cell, as printed")
    detail: str | None = Field(
        default=None,
        description="Sub-line printed under the row (bill reference, installment note, exchange note), redacted",
    )

    @property
    def signed_amount(self) -> int:
        """Debit positive, credit negative."""
        return self.amount if self.direction == "debit" else -self.amount


class Reconciliation(_Model):
    status: ReconciliationStatus
    expected: int
    computed: int
    difference: int
    checks: list[ReconciliationCheck] = Field(default_factory=list)


class ReconciliationCheck(_Model):
    name: str
    status: ReconciliationStatus
    expected: int
    computed: int
    difference: int


class PrintedSummary(_Model):
    """The printed summary block (``HESAP ÖZETİ``), minor units, signed as debt.

    ``interest_and_fees`` is ``None`` when the bank prints no separate fee line and counts interest and
    fees inside ``period_transactions`` (Yapı Kredi). ``remaining_installments`` is the printed total of
    installments still to come after this statement, when the bank prints it.
    """

    previous_balance: int
    period_transactions: int
    interest_and_fees: int | None
    payments: int
    statement_total: int
    remaining_installments: int | None = None


class StatementSummary(_Model):
    id: str
    bank: str
    product: str
    card_last4: str | None
    period_start: date
    period_end: date
    due_date: date
    currency: str = "TRY"
    statement_total: int
    minimum_payment: int
    reconciliation: Reconciliation | None = None
    analysis_status: AnalysisStatus = "none"
    needs_review: bool = Field(default=False, description="Read by the AI fallback and does not reconcile")


class Statement(StatementSummary):
    transactions: list[Transaction] = Field(default_factory=list)
    interest_rates: dict[str, str] = Field(default_factory=dict)
    parser_id: str | None = None
    credit_limit: int | None = None
    next_period_end: date | None = None
    next_due_date: date | None = None
    printed_summary: PrintedSummary | None = None
    page_count: int | None = None

    @property
    def cut_off_month(self) -> str:
        return f"{self.period_end.year:04d}-{self.period_end.month:02d}"


class ManualInstallment(_Model):
    """A manually entered installment plan (``POST /transactions`` with ``installment``)."""

    id: str
    description: str
    start_date: date
    total_count: int = Field(ge=1)
    amount_each: int = Field(ge=0)
    currency: str = "TRY"
    kind: Literal["purchase", "cash_advance"] = "purchase"


class FixedCost(_Model):
    """A recurring fixed cost (rent, dues) paid every month on ``day`` from ``start`` to ``end``."""

    id: str
    name: str = Field(min_length=1, max_length=120)
    amount: int = Field(gt=0, description="Minor units")
    currency: str = Field(default="TRY", pattern=r"^[A-Z]{3}$")
    day: int = Field(ge=1, le=31, description="Day of the month; clipped to the month's length")
    start: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="First month, YYYY-MM")
    end: str | None = Field(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Last month, inclusive")
    category: str | None = None


Reconciliation.model_rebuild()
