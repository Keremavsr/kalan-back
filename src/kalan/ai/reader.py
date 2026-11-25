"""AI fallback reader: when no parser or template matches, the model transcribes the statement.

The model gets the redacted page text and must answer with strict JSON: header fields, one entry per
printed row (date, description, amount as printed, debit/credit, installment numbers) and the totals
the bank printed. It transcribes; it never computes. Code converts every amount to minor units, builds
the installment plans, and runs the same reconciliation as for parsed statements. A statement that
does not reconcile is stored with ``needs_review: true``.

The same text can be turned into a draft YAML template (:func:`draft_template`) that the user checks
with ``kalan template check`` and may contribute.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kalan.ai.adapters import DEFAULT_TIMEOUT, Adapter, AdapterError
from kalan.ai.prompt import InvalidModelOutputError, _json_object, retry_prompt
from kalan.amounts import ISO_CURRENCY_RE, add_months, decimal_to_minor, month_key
from kalan.models import InstallmentInfo, PrintedSummary, Statement, Transaction, TransactionKind
from kalan.parsers.base import StatementParser
from kalan.parsers.template import BUILTIN_DIR, Template, TemplateParser
from kalan.reconcile import reconcile
from kalan.redact import redact
from kalan.text import fold, merchant_key, normalize_merchant

log = logging.getLogger(__name__)

PARSER_ID = "ai-fallback"
MAX_TEXT_CHARS = 60_000
_DECIMAL = r"^-?\d+(?:\.\d{1,3})?$"
_KINDS = ("purchase", "installment", "cash_advance", "fee", "tax", "interest", "payment", "refund", "carry_over",
          "other")


class TextTooLongError(ValueError):
    """The statement text is too long to send in one call."""


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore")


class AIInstallment(_M):
    index: int | None = Field(default=None, ge=1, le=99)
    count: int | None = Field(default=None, ge=1, le=99)
    remaining_count: int | None = Field(default=None, ge=0, le=99)
    amount_each: str | None = Field(default=None, pattern=_DECIMAL)


class AIRow(_M):
    date: date
    description: str = Field(min_length=1, max_length=300)
    amount: str = Field(pattern=_DECIMAL, description="positive decimal as printed")
    sign: Literal["debit", "credit"]
    kind: str = "other"
    installment: AIInstallment | None = None
    original_amount: str | None = Field(default=None, pattern=_DECIMAL)
    original_currency: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        return v if v in _KINDS else "other"

    @field_validator("original_currency")
    @classmethod
    def _cur(cls, v: str | None) -> str | None:
        return v.upper() if v and ISO_CURRENCY_RE.match(v.upper()) else None


class AITotals(_M):
    previous_balance: str = Field(pattern=_DECIMAL)
    period_transactions: str = Field(pattern=_DECIMAL)
    interest_and_fees: str = Field(pattern=_DECIMAL)
    payments: str = Field(pattern=_DECIMAL)
    statement_total: str = Field(pattern=_DECIMAL)


class AIStatement(_M):
    bank: str | None = Field(default=None, max_length=80)
    product: str | None = Field(default=None, max_length=80)
    country: str | None = None
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    period_start: date | None = None
    period_end: date
    due_date: date
    statement_total: str = Field(pattern=_DECIMAL)
    minimum_payment: str = Field(pattern=_DECIMAL)
    credit_limit: str | None = Field(default=None, pattern=_DECIMAL)
    rows: list[AIRow] = Field(max_length=2000)
    printed_totals: AITotals | None = None


_SHAPE = {
    "bank": "bank name as printed, or null",
    "product": "card product name, or null",
    "country": "ISO 3166-1 alpha-2 country code, or null",
    "currency": "ISO 4217 code of the statement, e.g. TRY, BRL, MXN, ARS, ILS, USD",
    "period_start": "YYYY-MM-DD or null",
    "period_end": "YYYY-MM-DD (statement cut-off date)",
    "due_date": "YYYY-MM-DD",
    "statement_total": "decimal, e.g. 1234.56 (negative if the balance is in the holder's favour)",
    "minimum_payment": "decimal",
    "credit_limit": "decimal or null",
    "rows": [{
        "date": "YYYY-MM-DD",
        "description": "exactly as printed",
        "amount": "positive decimal as printed, e.g. 49.90",
        "sign": "debit|credit",
        "kind": "|".join(_KINDS),
        "installment": {"index": "k of 'k/n' or null", "count": "n or null", "remaining_count": "int or null",
                        "amount_each": "decimal or null"},
        "original_amount": "foreign amount as printed, decimal, or null",
        "original_currency": "ISO 4217 or null",
    }],
    "printed_totals": {
        "previous_balance": "decimal", "period_transactions": "decimal", "interest_and_fees": "decimal",
        "payments": "positive decimal", "statement_total": "decimal",
    },
}


def _checked_text(text: str) -> str:
    safe = redact(text).text  # second pass: the caller already redacted, this is the line of defence
    if len(safe) > MAX_TEXT_CHARS:
        raise TextTooLongError(f"statement text has {len(safe)} characters, the limit is {MAX_TEXT_CHARS}")
    return safe


def build_reader_prompt(text: str) -> str:
    return "\n".join([
        "You transcribe one credit card statement into JSON. Personal data was already removed.",
        "Copy what is printed. Do not compute, add up, correct or invent anything; code checks your numbers.",
        "Rules:",
        "- One entry in rows per printed transaction row, in printed order, including the previous-balance row "
        "(kind carry_over) and payments (kind payment, sign credit) if they are printed as rows.",
        "- Amounts: plain decimals with a dot and no thousands separator (2.345,60 becomes 2345.60).",
        "- sign: credit for payments, refunds and anything printed as a credit; debit otherwise.",
        "- installment: only when the row shows installment numbers (3/6, 3 of 6, 2x150,00, last installment).",
        "- kind: installment for an installment of a purchase; fee for fees and card charges; interest for "
        "interest; tax for taxes; cash_advance for cash withdrawals.",
        "- printed_totals: the statement's own summary block (previous balance, period transactions, interest "
        "and fees, payments, total), or null if it has none. previous_balance + period_transactions + "
        "interest_and_fees - payments = statement_total on a correct summary.",
        "",
        "Answer with ONE JSON object only: no prose, no markdown fences. Exact shape:",
        json.dumps(_SHAPE, ensure_ascii=False),
        "",
        "Statement text (pages separated by form feeds):",
        text,
    ])


def _slug(value: str | None, default: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", fold(value or ""))
    return s[:40] or default


def to_statement(answer: AIStatement, card_last4: str | None) -> Statement:
    """Code part of the fallback: minor units, plans, reconciliation. Raises ``ValueError`` on bad amounts."""
    cur = answer.currency
    bank = _slug(answer.bank, "unknown")
    st_id = StatementParser.stable_id("st", bank, card_last4, answer.period_end.isoformat(), PARSER_ID)
    cut_month = month_key(answer.period_end)
    out: list[Transaction] = []
    for n, row in enumerate(answer.rows):
        amount = abs(decimal_to_minor(row.amount, cur))
        kind: TransactionKind = row.kind  # type: ignore[assignment]
        installment = None
        inst = row.installment
        if inst is not None and (inst.index is not None or inst.remaining_count is not None):
            remaining = inst.remaining_count
            if remaining is None and inst.index is not None and inst.count is not None:
                remaining = max(inst.count - inst.index, 0)
            remaining = remaining or 0
            each = abs(decimal_to_minor(inst.amount_each, cur)) if inst.amount_each else (amount if remaining else 0)
            total = inst.count or (inst.index + remaining if inst.index is not None else None)
            if total is not None and inst.index is not None and inst.index > total:
                total = None
            stem = fold(merchant_key(row.description))
            if inst.index is not None:
                anchor = f"start:{add_months(cut_month, -(inst.index - 1))}:of:{total}"
            else:
                anchor = f"amount:{each or amount}"
            plan_kind = "cash_advance" if kind == "cash_advance" else "purchase"
            if kind in ("purchase", "other"):
                kind = "installment"
            installment = InstallmentInfo(
                remaining_count=remaining,
                remaining_amount_each=each if remaining else 0,
                is_last=remaining == 0,
                plan_id=StatementParser.stable_id("pl", bank, card_last4, plan_kind, stem, anchor),
                index=inst.index,
                total_count=total,
            )
        foreign = row.original_amount is not None and row.original_currency is not None
        out.append(Transaction(
            id=StatementParser.stable_id("tx", st_id, n),
            date=row.date,
            description=row.description,
            amount=amount,
            currency=cur,
            direction=row.sign,
            kind=kind,
            installment=installment,
            merchant=normalize_merchant(row.description) if kind in ("purchase", "installment", "refund") else None,
            source="statement",
            original_amount=abs(decimal_to_minor(row.original_amount, row.original_currency)) if foreign else None,
            original_currency=row.original_currency if foreign else None,
        ))
    summary = None
    if answer.printed_totals is not None:
        t = answer.printed_totals
        summary = PrintedSummary(
            previous_balance=decimal_to_minor(t.previous_balance, cur),
            period_transactions=decimal_to_minor(t.period_transactions, cur),
            interest_and_fees=decimal_to_minor(t.interest_and_fees, cur),
            payments=abs(decimal_to_minor(t.payments, cur)),
            statement_total=decimal_to_minor(t.statement_total, cur),
        )
    period_start = answer.period_start
    if period_start is None:
        carry = next((t for t in out if t.kind == "carry_over"), None)
        period_start = date.fromordinal(carry.date.toordinal() + 1) if carry else answer.period_end
    st = Statement(
        id=st_id,
        bank=bank,
        product=(answer.product or "unknown")[:80],
        card_last4=card_last4,
        period_start=min(period_start, answer.period_end),
        period_end=answer.period_end,
        due_date=answer.due_date,
        currency=cur,
        statement_total=decimal_to_minor(answer.statement_total, cur),
        minimum_payment=abs(decimal_to_minor(answer.minimum_payment, cur)),
        transactions=out,
        parser_id=PARSER_ID,
        credit_limit=abs(decimal_to_minor(answer.credit_limit, cur)) if answer.credit_limit else None,
        printed_summary=summary,
    )
    rec = reconcile(st)
    return st.model_copy(update={"reconciliation": rec, "needs_review": rec.status != "ok"})


def parse_reader_answer(text: str, card_last4: str | None) -> Statement:
    data = _json_object(text)
    try:
        answer = AIStatement.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise InvalidModelOutputError(
            f"schema mismatch at {'.'.join(str(p) for p in first['loc'])}: {first['msg']}"
        ) from None
    try:
        return to_statement(answer, card_last4)
    except ValueError as exc:
        raise InvalidModelOutputError(str(exc)) from None


def _ask(adapter: Adapter, prompt: str, timeout: float, parse: Any) -> Any:
    """One call and one retry if the answer does not validate."""
    for attempt in (1, 2):
        text = adapter.complete(prompt, timeout)
        try:
            return parse(text)
        except InvalidModelOutputError as exc:
            log.warning("invalid answer from %s (attempt %d)", adapter.id, attempt)
            if attempt == 2:
                raise
            prompt = retry_prompt(prompt, str(exc))
    raise AssertionError("unreachable")


def read_statement(adapter: Adapter, text: str, card_last4: str | None, timeout: float = DEFAULT_TIMEOUT) -> Statement:
    """Fallback read of redacted statement text. Raises AdapterError, InvalidModelOutputError, TextTooLongError."""
    prompt = build_reader_prompt(_checked_text(text))
    st = _ask(adapter, prompt, timeout, lambda answer: parse_reader_answer(answer, card_last4))
    log.info("ai fallback via %s: %d rows, reconciliation %s", adapter.id, len(st.transactions),
             st.reconciliation.status if st.reconciliation else "none")
    return st


# ------------------------------------------------------------------------------------ template draft

_FENCE = re.compile(r"^```(?:ya?ml)?\s*|\s*```$", re.IGNORECASE)


def build_template_prompt(text: str, error: str | None = None) -> str:
    example = (BUILTIN_DIR / "tr_vakifbank_worldcard.yaml").read_text(encoding="utf-8")
    return "\n".join([
        "Write a kalan bank template (YAML) that parses the statement text below.",
        "Follow the structure of the example template exactly; keep only keys it uses. Replace every label,",
        "marker and regex with what THIS statement prints. Rules:",
        "- id is <country>_<bank>_<product> in lower case ASCII (ISO 3166 country code).",
        "- Labels and kind regexes are matched after folding: lower case, no accents, dotted/undotted I unified.",
        "- header.layout: colon_line (label, ':' and value on three lines), next_line or inline ('Label: value').",
        "- table.layout: cells (one cell per line) or line (one row per line, give row_pattern with named groups "
        "date, description, amount and optional installment).",
        "- Do not copy any merchant, amount, name or number from the statement into the template.",
        "Answer with the YAML only, no prose and no markdown fences.",
        "",
        "Example template:",
        example,
        "",
        "Statement text (pages separated by form feeds):",
        text,
        *(["", f"Your previous template was rejected: {error[:500]}", "Fix it and answer again."] if error else []),
    ])


def _parse_template(text: str) -> tuple[str, Template]:
    body = _FENCE.sub("", text.strip())
    try:
        raw = yaml.safe_load(body)
    except yaml.YAMLError as exc:
        raise InvalidModelOutputError(f"not valid YAML: {str(exc).splitlines()[0]}") from None
    if not isinstance(raw, dict):
        raise InvalidModelOutputError("the answer is not a YAML mapping")
    try:
        return body + "\n", Template.model_validate(raw)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:8])
        raise InvalidModelOutputError(f"template does not validate: {errors}") from None


def draft_template(adapter: Adapter, text: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Ask for a template draft and try it on the same text.

    Returns ``{"yaml", "valid", "errors", "detect", "parse_error", "reconciliation"}``. A draft that still
    does not validate after one retry is returned with ``valid: false`` and the errors.
    """
    safe = _checked_text(text)
    prompt = build_template_prompt(safe)
    body = ""
    for attempt in (1, 2):
        body = _FENCE.sub("", adapter.complete(prompt, timeout).strip()) + "\n"
        try:
            body, template = _parse_template(body)
            break
        except InvalidModelOutputError as exc:
            log.warning("invalid template draft from %s (attempt %d)", adapter.id, attempt)
            if attempt == 2:
                return {"yaml": body, "valid": False, "errors": [str(exc)], "detect": None, "parse_error": None,
                        "reconciliation": None}
            prompt = build_template_prompt(safe, str(exc))
    parser = TemplateParser(template)
    result: dict[str, Any] = {"yaml": body, "valid": True, "errors": [], "detect": parser.detect(safe),
                              "parse_error": None, "reconciliation": None}
    try:
        st = parser.parse(safe)
    except Exception as exc:  # the draft is the model's; report, never raise
        result["parse_error"] = type(exc).__name__ if not str(exc) else str(exc).split(":")[0]
        return result
    result["reconciliation"] = reconcile(st).model_dump()
    return result


__all__ = [
    "PARSER_ID",
    "AdapterError",
    "InvalidModelOutputError",
    "TextTooLongError",
    "draft_template",
    "parse_reader_answer",
    "read_statement",
]
