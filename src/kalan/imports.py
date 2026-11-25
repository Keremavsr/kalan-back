"""CSV, OFX and QIF import, parsed in code.

The result is a list of :class:`~kalan.models.Transaction` with ``source: "import"``. Nothing is guessed
silently: the detected delimiter, decimal format, date format and sign convention are returned with
the rows, and a ``mapping`` object overrides any of them.

Installment hints (``3/10``, ``parcela 3 de 10``, ``cuota 3 de 6``) come from an installment column or
from the description, using a template's installment and kind rules when ``mapping.template`` names
one, otherwise a small generic rule set.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kalan.amounts import (
    COMMA_DECIMAL,
    DOT_DECIMAL,
    ISO_CURRENCY_RE,
    SPACE_COMMA,
    AmountFormat,
    add_months,
    minor_units,
    month_key,
    try_parse_amount,
    try_parse_date,
)
from kalan.models import InstallmentInfo, ManualInstallment, Transaction, TransactionKind
from kalan.parsers.base import StatementParser
from kalan.parsers.template import KindRule
from kalan.redact import redact_text
from kalan.text import fold, merchant_key, normalize_merchant

ImportFormat = Literal["csv", "ofx", "qif"]
SignRule = Literal["negative_debit", "positive_debit"]


class ImportFileError(ValueError):
    """The file cannot be imported; the message says why (no file content in it)."""


class Mapping(BaseModel):
    """Optional overrides for an import. Column names are matched after fold()."""

    model_config = ConfigDict(extra="forbid")
    date: str | None = None
    description: str | None = None
    amount: str | None = None
    debit: str | None = None
    credit: str | None = None
    installment: str | None = None
    currency_column: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)
    decimal: Literal[".", ","] | None = None
    date_format: str | None = Field(default=None, description="e.g. DD/MM/YYYY, MM/DD/YYYY, YYYY-MM-DD")
    sign: SignRule | None = Field(default=None, description="Which sign of the amount column is a charge")
    template: str | None = Field(default=None, description="Template id whose kind and installment rules apply")


@dataclass
class ImportResult:
    format: ImportFormat
    currency: str
    transactions: list[Transaction]
    detected: dict[str, object] = field(default_factory=dict)

    @property
    def months(self) -> list[str]:
        return sorted({month_key(t.date) for t in self.transactions})


# -- column names in the languages kalan targets (compared after fold()) -------------------------

_SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("date", "transaction date", "posted date", "posting date", "data", "data da compra", "data lancamento",
             "fecha", "fecha de compra", "fecha operacion", "tarih", "islem tarihi", "תאריך", "תאריך עסקה"),
    "description": ("description", "details", "merchant", "payee", "name", "title", "memo", "descricao",
                    "descrição", "estabelecimento", "lancamento", "historico", "titulo", "descripcion", "concepto",
                    "comercio", "detalle", "aciklama", "islem", "תיאור", "שם בית העסק", "בית עסק"),
    "amount": ("amount", "value", "valor", "valor (r$)", "valor em r$", "importe", "monto", "tutar", "tutar (tl)",
               "סכום", "סכום חיוב", "charge amount"),
    "debit": ("debit", "debito", "cargo", "cargos", "borc", "withdrawal", "חובה"),
    "credit": ("credit", "credito", "abono", "abonos", "alacak", "deposit", "זכות"),
    "installment": ("installment", "installments", "parcela", "parcelas", "cuota", "cuotas", "taksit",
                    "kalan taksit", "תשלום", "תשלומים"),
    "currency_column": ("currency", "moeda", "moneda", "para birimi", "doviz", "מטבע"),
}

_GENERIC_INDEX = [
    re.compile(r"\b(?:parcela|parc\.?|cuota|taksit|installment|inst\.?)\s*(?P<k>\d{1,2})\s*(?:/|de|of|-)\s*(?P<n>\d{1,2})\b"),
    re.compile(r"\b(?P<k>\d{1,2})\s*/\s*(?P<n>\d{1,2})\b"),
    re.compile(r"\b(?P<k>\d{1,2})\s*(?:de|of)\s*(?P<n>\d{1,2})\b"),
]
_CELL_INDEX = re.compile(r"^\s*(?P<k>\d{1,2})\s*(?:/|de|of|z|x|-)\s*(?P<n>\d{1,2})\s*$", re.IGNORECASE)
_GENERIC_KINDS = [
    KindRule(match=r"^(?:payment|pagamento|pago|pago recibido|pagamento recebido|odeme|odemeniz|תשלום)", credit=True,
             kind="payment", direction="credit"),
    KindRule(match=r"\b(?:interest|juros|interes|intereses|faiz|ריבית)\b", kind="interest"),
    KindRule(match=r"\b(?:annual fee|anuidade|tarifa|comision|comisión|aidat|ucret|fee|עמלה)\b", kind="fee"),
    KindRule(match=r"\b(?:iof|iva|tax|impuesto|vergi|bsmv|kkdf)\b", kind="tax"),
    KindRule(match=r"\b(?:saque|cash advance|avance|nakit)\b", kind="cash_advance"),
    KindRule(credit=True, kind="refund", direction="credit"),
    KindRule(installment=True, kind="installment"),
]

_DATE_CANDIDATES = ("D/M/YYYY", "M/D/YYYY", "YYYY-M-D", "D.M.YYYY", "D-M-YYYY", "YYYY/M/D", "D/M/YY", "M/D/YY",
                    "D.M.YY", "YYYYMMDD")
_AMOUNT_CANDIDATES = (
    ("dot_decimal", DOT_DECIMAL),
    ("comma_decimal", COMMA_DECIMAL),
    ("space_comma", SPACE_COMMA),
    ("plain_dot", AmountFormat(thousands="", decimal=".")),
    ("plain_comma", AmountFormat(thousands="", decimal=",")),
)


# -- detection helpers ----------------------------------------------------------------------------


def _clean_amount(text: str) -> str:
    t = text.strip().replace(" ", " ")
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    return t


def detect_amount_format(samples: list[str], decimal: str | None = None) -> tuple[str, AmountFormat]:
    """The format that parses every sample; ties go to the one that finds decimals in more samples."""
    values = [_clean_amount(s) for s in samples if s.strip()]
    if not values:
        return "dot_decimal", DOT_DECIMAL
    best: tuple[int, str, AmountFormat] | None = None
    for name, fmt in _AMOUNT_CANDIDATES:
        if decimal is not None and fmt.decimal != decimal:
            continue
        if not all(try_parse_amount(v, fmt) is not None for v in values):
            continue
        with_decimals = sum(1 for v in values if re.search(rf"{re.escape(fmt.decimal)}\d{{1,2}}\D*$", v))
        if best is None or with_decimals > best[0]:
            best = (with_decimals, name, fmt)
    if best is None:
        raise ImportFileError("amounts are not in one consistent number format")
    return best[1], best[2]


def _date_part(text: str) -> str:
    t = text.strip()
    return re.split(r"[T ]", t, maxsplit=1)[0] if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", t) else t


def detect_date_format(samples: list[str], prefer: str | None = None) -> tuple[str, bool]:
    """Return ``(format, ambiguous)``: the first candidate that parses every sample.

    ``ambiguous`` is true when both day-first and month-first parse all samples (every day <= 12); then
    day-first wins unless ``prefer`` is given.
    """
    values = [_date_part(s) for s in samples if s.strip()]
    if prefer:
        if not all(try_parse_date(v, prefer) for v in values):
            raise ImportFileError(f"dates do not match {prefer}")
        return prefer, False
    fits = [f for f in _DATE_CANDIDATES if values and all(try_parse_date(v, f) for v in values)]
    if not fits:
        raise ImportFileError("dates are not in one known format; give mapping.date_format")
    pairs = {("D/M/YYYY", "M/D/YYYY"), ("D/M/YY", "M/D/YY")}
    ambiguous = any(a in fits and b in fits for a, b in pairs)
    return fits[0], ambiguous


def _column(headers: list[str], role: str, given: str | None) -> int | None:
    folded = [fold(h) for h in headers]
    if given is not None:
        if fold(given) not in folded:
            raise ImportFileError(f"mapping column {given!r} is not in the header")
        return folded.index(fold(given))
    for name in _SYNONYMS[role]:
        if fold(name) in folded:
            return folded.index(fold(name))
    return None


# -- shared row building --------------------------------------------------------------------------


@dataclass
class _Raw:
    date: date
    description: str
    minor: int  # signed: positive = charge (debit)
    installment: str | None = None
    currency: str | None = None
    uid: str | None = None


class _Rules:
    def __init__(self, template_id: str | None) -> None:
        self.parser = None
        if template_id is not None:
            from kalan.parsers import UnknownFormatError, get_parser
            from kalan.parsers.template import TemplateParser

            try:
                parser = get_parser(template_id)
            except UnknownFormatError:
                raise ImportFileError(f"no template called {template_id!r}") from None
            if not isinstance(parser, TemplateParser):
                raise ImportFileError(f"{template_id!r} is a Python parser, not a template")
            self.parser = parser

    def index(self, description: str, cell: str | None) -> tuple[int, int] | None:
        if cell:
            m = _CELL_INDEX.match(cell)
            if m and 1 <= int(m["k"]) <= int(m["n"]):
                return int(m["k"]), int(m["n"])
        if self.parser is not None:
            k, n = self.parser._installment_index(description)
            return (k, n) if k is not None and n is not None else None
        d = fold(description)
        for p in _GENERIC_INDEX:
            m = p.search(d)
            if m and 1 <= int(m["k"]) <= int(m["n"]) and int(m["n"]) > 1:
                return int(m["k"]), int(m["n"])
        return None

    def kind(self, description: str, credit: bool, has_installment: bool) -> tuple[TransactionKind, str]:
        if self.parser is not None:
            kind, direction, _ = self.parser.classify(description, credit, has_installment)
            return kind, direction
        d = fold(description)
        for rule in _GENERIC_KINDS:
            if rule.match is not None and not re.search(rule.match, d):
                continue
            if rule.credit is not None and rule.credit != credit:
                continue
            if rule.installment is not None and rule.installment != has_installment:
                continue
            return rule.kind, rule.direction or ("credit" if credit else "debit")
        return ("refund", "credit") if credit else ("purchase", "debit")


def _stem(description: str) -> str:
    d = fold(merchant_key(description))
    for p in _GENERIC_INDEX:
        d = p.sub(" ", d)
    return re.sub(r"\s+", " ", d).strip(" -/.")


def build_transactions(raws: list[_Raw], currency: str, rules: _Rules) -> list[Transaction]:
    out: list[Transaction] = []
    seen: dict[str, int] = {}
    for r in raws:
        r.description = redact_text(r.description)
        credit = r.minor < 0
        idx = rules.index(r.description, r.installment)
        kind, direction = rules.kind(r.description, credit, idx is not None)
        amount = abs(r.minor)
        cur = r.currency or currency
        key = r.uid or "|".join((r.date.isoformat(), r.description, str(r.minor), cur))
        seen[key] = seen.get(key, 0) + 1
        tx_id = StatementParser.stable_id("tx", "import", key, seen[key])
        info = None
        if idx is not None and direction == "debit":
            k, n = idx
            start = add_months(month_key(r.date), -(k - 1))
            info = InstallmentInfo(
                remaining_count=n - k,
                remaining_amount_each=amount if n > k else 0,
                is_last=k == n,
                plan_id=StatementParser.stable_id("pl", "import", _stem(r.description), start, n, amount),
                index=k,
                total_count=n,
            )
            if kind == "purchase":
                kind = "installment"
        out.append(Transaction(
            id=tx_id,
            date=r.date,
            description=r.description,
            amount=amount,
            currency=cur,
            direction=direction,  # type: ignore[arg-type]
            kind=kind,
            installment=info,
            merchant=normalize_merchant(r.description) if kind in ("purchase", "installment", "refund") else None,
            source="import",
        ))
    return out


def import_plans(transactions: list[Transaction]) -> list[ManualInstallment]:
    """Installment plans implied by imported rows, one per plan id, from the row with the highest index."""
    best: dict[str, Transaction] = {}
    for tx in transactions:
        i = tx.installment
        if i is None or i.index is None or i.total_count is None:
            continue
        cur = best.get(i.plan_id)
        if cur is None or (cur.installment and cur.installment.index and i.index > cur.installment.index):
            best[i.plan_id] = tx
    plans = []
    for plan_id, tx in sorted(best.items()):
        i = tx.installment
        assert i is not None and i.index is not None and i.total_count is not None
        y, m = (int(p) for p in add_months(month_key(tx.date), -(i.index - 1)).split("-"))
        day = min(tx.date.day, 28)
        plans.append(ManualInstallment(
            id=plan_id, description=_stem(tx.description).upper() or tx.description, start_date=date(y, m, day),
            total_count=i.total_count, amount_each=tx.amount, currency=tx.currency,
        ))
    return plans


# -- CSV ------------------------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1254", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFileError("unknown text encoding")


def parse_csv(data: bytes, mapping: Mapping, default_currency: str) -> ImportResult:
    text = _decode(data)
    sample = text[:20_000]
    if mapping.delimiter:
        delimiter = mapping.delimiter
    else:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ImportFileError("the file has no data rows")

    header_at = None
    for n, row in enumerate(rows[:15]):
        folded = {fold(c) for c in row}
        if any(fold(s) in folded for s in _SYNONYMS["date"]) or (mapping.date and fold(mapping.date) in folded):
            header_at = n
            break
    if header_at is None:
        raise ImportFileError("no header row with a date column found; give mapping.date")
    headers = [c.strip() for c in rows[header_at]]
    body = rows[header_at + 1 :]

    cols = {role: _column(headers, role, getattr(mapping, role)) for role in _SYNONYMS}
    if cols["date"] is None or cols["description"] is None:
        raise ImportFileError("date and description columns are required")
    if cols["amount"] is None and cols["debit"] is None and cols["credit"] is None:
        raise ImportFileError("an amount column, or debit and credit columns, is required")

    def cell(row: list[str], role: str) -> str:
        i = cols[role]
        return row[i].strip() if i is not None and i < len(row) else ""

    money_cells = [cell(r, k) for r in body for k in ("amount", "debit", "credit") if cell(r, k)]
    fmt_name, fmt = detect_amount_format(money_cells, mapping.decimal)
    date_fmt, ambiguous = detect_date_format([cell(r, "date") for r in body], mapping.date_format)

    signed: list[tuple[list[str], int]] = []
    for row in body:
        if cols["amount"] is not None and cell(row, "amount"):
            p = try_parse_amount(_clean_amount(cell(row, "amount")), fmt)
            if p is None:
                raise ImportFileError("an amount does not fit the detected format")
            signed.append((row, p.signed))
        else:
            debit = try_parse_amount(_clean_amount(cell(row, "debit") or "0"), fmt)
            credit = try_parse_amount(_clean_amount(cell(row, "credit") or "0"), fmt)
            if debit is None or credit is None:
                raise ImportFileError("a debit or credit amount does not fit the detected format")
            signed.append((row, abs(debit.minor) - abs(credit.minor)))

    sign = mapping.sign
    if sign is None:
        if cols["amount"] is None:
            sign = "positive_debit"
        else:
            negatives = sum(1 for _, v in signed if v < 0)
            sign = "negative_debit" if negatives > len(signed) / 2 else "positive_debit"
    factor = -1 if sign == "negative_debit" and cols["amount"] is not None else 1

    currency = mapping.currency or default_currency
    raws = []
    for row, value in signed:
        d = try_parse_date(_date_part(cell(row, "date")), date_fmt)
        if d is None:
            raise ImportFileError("a date does not fit the detected format")
        cur = cell(row, "currency_column").upper() or None
        if cur is not None and not ISO_CURRENCY_RE.match(cur):
            cur = None
        raws.append(_Raw(
            date=d, description=re.sub(r"\s+", " ", cell(row, "description")) or "-",
            minor=_rescale(value * factor, fmt.decimals, cur or currency),
            installment=cell(row, "installment") or None, currency=cur,
        ))
    txs = build_transactions(raws, currency, _Rules(mapping.template))
    detected = {
        "delimiter": delimiter, "decimal": fmt.decimal, "amount_format": fmt_name, "date_format": date_fmt,
        "date_ambiguous": ambiguous, "sign": sign,
        "columns": {role: headers[i] for role, i in cols.items() if i is not None},
    }
    return ImportResult("csv", currency, txs, detected)


def _rescale(minor: int, decimals: int, currency: str) -> int:
    """Amount parsed with ``decimals`` places -> minor units of ``currency``."""
    target = minor_units(currency)
    if target == decimals:
        return minor
    value = Decimal(minor) / (10**decimals) * (10**target)
    if value != value.to_integral_value():
        raise ImportFileError(f"an amount has more decimals than {currency} allows")
    return int(value)


# -- OFX ------------------------------------------------------------------------------------------

_OFX_TAG = re.compile(r"<(?P<tag>[A-Z0-9.]+)>(?P<value>[^<\r\n]*)", re.IGNORECASE)


def parse_ofx(data: bytes, mapping: Mapping, default_currency: str) -> ImportResult:
    """OFX 1.x (SGML, tags may be unclosed) and 2.x (XML). TRNAMT is signed: negative = charge."""
    text = _decode(data)
    cur_m = re.search(r"<CURDEF>\s*([A-Za-z]{3})", text, re.IGNORECASE)
    currency = mapping.currency or (cur_m.group(1).upper() if cur_m else default_currency)
    blocks = re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|</BANKTRANLIST>)", text, re.IGNORECASE | re.S)
    if not blocks:
        raise ImportFileError("no OFX transactions (STMTTRN) found")
    raws = []
    for block in blocks:
        tags = {m["tag"].upper(): m["value"].strip() for m in _OFX_TAG.finditer(block)}
        if "DTPOSTED" not in tags or "TRNAMT" not in tags:
            raise ImportFileError("an OFX transaction has no DTPOSTED or TRNAMT")
        d = try_parse_date(tags["DTPOSTED"][:8], "YYYYMMDD")
        amount = tags["TRNAMT"].replace(",", ".")
        if d is None or not re.match(r"^[+-]?\d+(?:\.\d+)?$", amount):
            raise ImportFileError("an OFX date or amount is malformed")
        value = Decimal(amount) * (10 ** minor_units(currency))
        if value != value.to_integral_value():
            raise ImportFileError(f"an amount has more decimals than {currency} allows")
        name, memo = tags.get("NAME", ""), tags.get("MEMO", "")
        parts = [name] + ([memo] if memo and memo != name else [])
        description = " ".join(p for p in parts if p) or "-"
        raws.append(_Raw(date=d, description=re.sub(r"\s+", " ", description), minor=-int(value),
                         uid=f"ofx:{tags['FITID']}" if tags.get("FITID") else None))
    txs = build_transactions(raws, currency, _Rules(mapping.template))
    return ImportResult("ofx", currency, txs, {"sign": "negative_debit", "date_format": "YYYYMMDD"})


# -- QIF ------------------------------------------------------------------------------------------


def parse_qif(data: bytes, mapping: Mapping, default_currency: str) -> ImportResult:
    """QIF records (D date, T/U amount, P payee, M memo, ^ end). Negative amount = charge."""
    text = _decode(data)
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("!"):
            continue
        if line == "^":
            if current:
                records.append(current)
            current = {}
            continue
        code, value = line[0], line[1:].strip()
        current.setdefault(code, value)
    if current:
        records.append(current)
    records = [r for r in records if "D" in r and ("T" in r or "U" in r)]
    if not records:
        raise ImportFileError("no QIF transactions found")
    dates = [re.sub(r"^(\d{1,2}/\d{1,2}/)(\d{2})$", r"\g<1>20\g<2>", r["D"].replace("'", "/").replace(" ", ""))
             for r in records]
    date_fmt, ambiguous = detect_date_format(dates, mapping.date_format)
    amounts = [r.get("T") or r["U"] for r in records]
    fmt_name, fmt = detect_amount_format(amounts, mapping.decimal)
    currency = mapping.currency or default_currency
    sign = mapping.sign or "negative_debit"
    raws = []
    for r, ds, a in zip(records, dates, amounts, strict=True):
        d = try_parse_date(ds, date_fmt)
        p = try_parse_amount(_clean_amount(a), fmt)
        if d is None or p is None:
            raise ImportFileError("a QIF date or amount is malformed")
        value = -p.signed if sign == "negative_debit" else p.signed
        description = " ".join(x for x in (r.get("P", ""), r.get("M", "")) if x) or "-"
        raws.append(_Raw(date=d, description=description, minor=_rescale(value, fmt.decimals, currency)))
    txs = build_transactions(raws, currency, _Rules(mapping.template))
    return ImportResult("qif", currency, txs, {"date_format": date_fmt, "date_ambiguous": ambiguous,
                                              "amount_format": fmt_name, "sign": sign})


def sniff_format(filename: str | None, data: bytes) -> ImportFormat:
    head = data[:2000].decode("latin-1", errors="replace").lstrip().upper()
    name = (filename or "").lower()
    if name.endswith(".ofx") or name.endswith(".qfx") or "OFXHEADER" in head or "<OFX>" in head:
        return "ofx"
    if name.endswith(".qif") or head.startswith("!TYPE") or head.startswith("!ACCOUNT"):
        return "qif"
    return "csv"


def parse_import(data: bytes, filename: str | None, mapping: Mapping | None, default_currency: str,
                 fmt: ImportFormat | None = None) -> ImportResult:
    mapping = mapping or Mapping()
    kind = fmt or sniff_format(filename, data)
    parser = {"csv": parse_csv, "ofx": parse_ofx, "qif": parse_qif}[kind]
    result = parser(data, mapping, default_currency)
    if not result.transactions:
        raise ImportFileError("the file has no transactions")
    return result
