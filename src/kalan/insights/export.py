"""Plain-text exports so kalan sits next to tools people already use: hledger, beancount, OFX and CSV.

Exported rows are every stored transaction (statements, manual entries, imports) except opening balance
lines (``carry_over``), which would count the previous statement twice. Installment plans go out as
hledger periodic transactions, beancount ``custom`` entries and a separate plans CSV.

Account names: ``liabilities:card:<bank>:<last four>`` (``manual`` / ``import`` for loose rows),
``expenses:<category or kind>`` for charges and refunds, ``assets:payments`` for card payments.
CSV and OFX are written so that kalan's own importers read them back unchanged (see the tests).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from decimal import Decimal
from typing import Literal
from xml.sax.saxutils import escape

from kalan.amounts import add_months, minor_units
from kalan.models import Statement, Transaction
from kalan.schedule import PlanSummary
from kalan.text import fold

ExportFormat = Literal["hledger", "beancount", "ofx", "csv"]
MEDIA_TYPES = {"hledger": "text/plain", "beancount": "text/plain", "ofx": "application/x-ofx", "csv": "text/csv"}
EXTENSIONS = {"hledger": "journal", "beancount": "beancount", "ofx": "ofx", "csv": "csv"}

Row = tuple[Transaction, str]  # transaction and its card account key


def export_rows(statements: list[Statement], loose: list[Transaction]) -> list[Row]:
    rows: list[Row] = []
    for st in statements:
        card = f"{st.bank}:{st.card_last4 or 'card'}"
        rows += [(t, card) for t in st.transactions if t.kind != "carry_over"]
    rows += [(t, t.source) for t in loose if t.kind != "carry_over"]
    return sorted(rows, key=lambda r: (r[0].date, r[0].id))


def _decimal(minor: int, currency: str) -> str:
    places = minor_units(currency)
    value = Decimal(minor) / (10**places)
    return f"{value:.{places}f}"


def _index(t: Transaction) -> str:
    info = t.installment
    if info is None or info.index is None or info.total_count is None:
        return ""
    return f"{info.index}/{info.total_count}"


def _other_account(t: Transaction) -> str:
    if t.kind == "payment":
        return "assets:payments"
    return f"expenses:{t.category or t.kind}"


# -- hledger --------------------------------------------------------------------------------------


def _ledger_account(name: str) -> str:
    return re.sub(r"\s{2,}|\t", " ", name).strip()


def to_hledger(rows: list[Row], plans: list[PlanSummary], currency: str) -> str:
    out = ["; kalan export (hledger journal). Amounts in the card's currency; charges increase the card liability.", ""]
    for t, card in rows:
        signed = t.signed_amount  # debit positive
        tags = [f"kind:{t.kind}", f"source:{t.source}", f"id:{t.id}"]
        if _index(t):
            tags.append(f"installment:{_index(t)}")
        out.append(f"{t.date.isoformat()} {t.description}  ; {', '.join(tags)}")
        out.append(f"    {_ledger_account(_other_account(t))}  {_decimal(signed, t.currency)} {t.currency}")
        out.append(f"    {_ledger_account('liabilities:card:' + card)}")
        out.append("")
    if plans:
        out.append("; Installment plans still to be billed (periodic transactions, hledger --forecast).")
        for p in plans:
            start, end = add_months(p.ends, -(p.remaining_count - 1)), add_months(p.ends, 1)
            out.append(f"~ monthly from {start}-01 to {end}-01  ; plan:{p.plan_id}, {p.description}")
            out.append(f"    expenses:installments  {_decimal(p.amount_each, currency)} {currency}")
            out.append("    liabilities:card:plans")
            out.append("")
    return "\n".join(out)


# -- beancount ------------------------------------------------------------------------------------


def _bean_account(name: str) -> str:
    parts = []
    for part in name.split(":"):
        words = [w for w in re.split(r"[^A-Za-z0-9]+", fold(part)) if w]
        parts.append("-".join(w.capitalize() for w in words) or "Unknown")
    out = []
    for part in parts:
        out.append(part if part[0].isalpha() else f"X{part}")
    return ":".join(out)


def _bean_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_beancount(rows: list[Row], plans: list[PlanSummary], currency: str) -> str:
    out = ["; kalan export (beancount). Charges are negative on the card liability.",
           f"option \"operating_currency\" {_bean_string(currency)}", ""]
    accounts: dict[str, date] = {}
    for t, card in rows:
        for acc in (_bean_account(_other_account(t)), _bean_account("liabilities:card:" + card)):
            accounts.setdefault(acc, t.date)
    if plans:
        first = rows[0][0].date if rows else date.today()
        accounts.setdefault(_bean_account("expenses:installments"), first)
    for acc, opened in sorted(accounts.items()):
        out.append(f"{opened.isoformat()} open {acc}")
    out.append("")
    for t, card in rows:
        out.append(f"{t.date.isoformat()} * {_bean_string(t.description)}")
        out.append(f"  kalan-id: {_bean_string(t.id)}")
        out.append(f"  kind: {_bean_string(t.kind)}")
        if _index(t):
            out.append(f"  installment: {_bean_string(_index(t))}")
        out.append(f"  {_bean_account(_other_account(t))}  {_decimal(t.signed_amount, t.currency)} {t.currency}")
        out.append(f"  {_bean_account('liabilities:card:' + card)}")
        out.append("")
    for p in plans:
        start = add_months(p.ends, -(p.remaining_count - 1))
        out.append(f"{start}-01 custom \"kalan-plan\" {_bean_string(p.plan_id)} {_bean_string(p.description)} "
                   f"{_decimal(p.amount_each, currency)} {currency} {p.remaining_count}")
    return "\n".join(out).rstrip() + "\n"


# -- OFX ------------------------------------------------------------------------------------------


def to_ofx(rows: list[Row], currency: str) -> tuple[str, int]:
    """OFX 2.2 credit card statement with the rows in ``currency``. Returns the text and the number of rows
    left out because they are in another currency. Charges are negative (``TRNAMT``), as OFX expects."""
    kept = [(t, c) for t, c in rows if t.currency == currency]
    lines = []
    for t, _card in kept:
        name = t.description
        idx = _index(t)
        memo = f"<MEMO>{escape(idx)}</MEMO>" if idx and idx not in name else ""
        lines.append(
            "<STMTTRN>"
            f"<TRNTYPE>{'DEBIT' if t.direction == 'debit' else 'CREDIT'}</TRNTYPE>"
            f"<DTPOSTED>{t.date.strftime('%Y%m%d')}</DTPOSTED>"
            f"<TRNAMT>{_decimal(-t.signed_amount, currency)}</TRNAMT>"
            f"<FITID>{escape(t.id)}</FITID><NAME>{escape(name)}</NAME>{memo}"
            "</STMTTRN>"
        )
    start = kept[0][0].date.strftime("%Y%m%d") if kept else "19700101"
    end = kept[-1][0].date.strftime("%Y%m%d") if kept else "19700101"
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
        "<OFX><CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>0</TRNUID>"
        "<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><CCSTMTRS>"
        f"<CURDEF>{currency}</CURDEF><CCACCTFROM><ACCTID>kalan</ACCTID></CCACCTFROM>"
        f"<BANKTRANLIST><DTSTART>{start}</DTSTART><DTEND>{end}</DTEND>\n"
        + "\n".join(lines)
        + "\n</BANKTRANLIST></CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1></OFX>\n"
    )
    return text, len(rows) - len(kept)


# -- CSV ------------------------------------------------------------------------------------------

CSV_COLUMNS = ("date", "description", "debit", "credit", "currency", "installment", "kind", "category", "source",
               "account", "id")
PLAN_COLUMNS = ("plan_id", "description", "kind", "source", "amount_each", "remaining_count", "ends",
                "total_remaining", "currency")


def to_csv(rows: list[Row]) -> str:
    """One row per transaction; ``debit`` / ``credit`` hold plain positive decimals (dot separator)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for t, card in rows:
        amount = _decimal(t.amount, t.currency)
        w.writerow([t.date.isoformat(), t.description, amount if t.direction == "debit" else "",
                    amount if t.direction == "credit" else "", t.currency, _index(t), t.kind, t.category or "",
                    t.source, card, t.id])
    return buf.getvalue()


def plans_csv(plans: list[PlanSummary], currency: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(PLAN_COLUMNS)
    for p in plans:
        w.writerow([p.plan_id, p.description, p.kind, p.source, _decimal(p.amount_each, currency), p.remaining_count,
                    p.ends, _decimal(p.total_remaining, currency), currency])
    return buf.getvalue()


def export(fmt: ExportFormat, statements: list[Statement], loose: list[Transaction], plans: list[PlanSummary],
           currency: str, what: Literal["transactions", "plans"] = "transactions") -> tuple[str, int]:
    """Text of the export and the number of rows left out (OFX keeps one currency only)."""
    rows = export_rows(statements, loose)
    if fmt == "csv":
        return (plans_csv(plans, currency), 0) if what == "plans" else (to_csv(rows), 0)
    if fmt == "ofx":
        return to_ofx(rows, currency)
    if fmt == "hledger":
        return to_hledger(rows, plans, currency), 0
    return to_beancount(rows, plans, currency), 0
