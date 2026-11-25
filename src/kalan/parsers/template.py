"""Declarative bank templates: a YAML file describes a statement layout, this engine parses it.

A template (``src/kalan/templates/*.yaml`` or ``<data dir>/templates/*.yaml``) declares how one bank
product prints its statement: detect markers, amount and date format, header labels, the transaction
table, installment cells, kind rules and the printed summary used for reconciliation. The engine is
the same code the VakıfBank parser used, with every bank-specific string moved into the template.
See ``docs/adding-a-bank.md`` and ``src/kalan/templates/template.schema.json``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kalan.amounts import (
    COMMA_DECIMAL,
    DOT_DECIMAL,
    SPACE_COMMA,
    AmountFormat,
    add_months,
    month_key,
    parse_amount,
    try_parse_amount,
    try_parse_date,
)
from kalan.models import (
    ComponentRole,
    Direction,
    InstallmentInfo,
    PrintedSummary,
    Statement,
    Transaction,
    TransactionKind,
)
from kalan.parsers.base import ParseError, StatementParser
from kalan.text import fold, merchant_fields

BUILTIN_DIR = Path(__file__).resolve().parent.parent / "templates"
SCHEMA_FILE = BUILTIN_DIR / "template.schema.json"
TEMPLATE_ID_RE = r"^[a-z]{2}_[a-z0-9]+_[a-z0-9]+$"

HeaderField = Literal[
    "statement_total",
    "minimum_payment",
    "due_date",
    "period_start",
    "period_end",
    "credit_limit",
    "next_period_end",
    "next_due_date",
    "card",
]
SummaryField = Literal["previous_balance", "period_transactions", "interest_and_fees", "payments", "statement_total"]
Column = Literal["date", "description", "amount", "installment", "points"]
REQUIRED_HEADER = ("statement_total", "minimum_payment", "due_date", "period_end")
_PRESETS = {"dot_decimal": DOT_DECIMAL, "comma_decimal": COMMA_DECIMAL, "space_comma": SPACE_COMMA}


def _compile(pattern: str, where: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{where}: bad regular expression {pattern!r}: {exc}") from None


# ------------------------------------------------------------------------------------------ schema


class _T(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AmountSpec(_T):
    """How amounts are printed. Either a preset or explicit separators."""

    preset: Literal["dot_decimal", "comma_decimal", "space_comma"] | None = Field(
        default=None, description="dot_decimal = 2,345.60; comma_decimal = 2.345,60; space_comma = 2 345,60"
    )
    thousands: str | None = Field(default=None, description='"," "." " " or "" (never grouped)')
    decimal: str | None = None
    decimals: int = Field(default=2, ge=0, le=3)
    credit_signs: list[str] = Field(
        default=["+", "-"], description="A printed sign from this list marks a credit (in the holder's favour)"
    )

    @model_validator(mode="after")
    def _one_way(self) -> AmountSpec:
        if self.preset is None and (self.thousands is None or self.decimal is None):
            raise ValueError("give either preset or both thousands and decimal")
        return self

    def format(self) -> AmountFormat:
        if self.preset is not None:
            base = _PRESETS[self.preset]
            return AmountFormat(thousands=base.thousands, decimal=base.decimal, decimals=self.decimals)
        return AmountFormat(thousands=self.thousands or "", decimal=self.decimal or ".", decimals=self.decimals)


class DetectSpec(_T):
    all: list[str] = Field(min_length=1, description="Every phrase must appear (compared after fold())")
    none: list[str] = Field(default_factory=list, description="No phrase may appear")


class HeaderSpec(_T):
    layout: Literal["colon_line", "next_line", "inline"] = Field(
        default="colon_line",
        description="colon_line: label / ':' / value on three lines; next_line: label then value; "
        "inline: 'label<separator> value' on one line",
    )
    separator: str = ":"
    fields: dict[HeaderField, list[str]] = Field(description="Field -> printed labels (any one matches)")
    card_last4_pattern: str = Field(
        default=r"(?:\*{2,}|x{2,}|X{2,}|\*\*\*\*\s?)(\d{4})\]?$",
        description="Regex on the card field; group 1 = last four digits",
    )

    @model_validator(mode="after")
    def _required(self) -> HeaderSpec:
        missing = [f for f in REQUIRED_HEADER if f not in self.fields]
        if missing:
            raise ValueError("header.fields must include " + ", ".join(missing))
        return self


class TableSpec(_T):
    header: list[str] = Field(min_length=1, description="Table header labels, one per line, in order")
    end: list[str] = Field(min_length=1, description="First line after the table (any one of these labels)")
    skip_lines: list[str] = Field(default_factory=list, description="Lines to drop inside the table (page titles)")
    skip_patterns: list[str] = Field(
        default_factory=list, description="Regexes on fold(line) to drop inside the table (page footers)"
    )
    layout: Literal["cells", "line"] = Field(
        default="cells",
        description="cells: every cell on its own line (date, description lines, amount, optional cells); "
        "line: one row per line, parsed with row_pattern",
    )
    columns: list[Column] = Field(
        default=["date", "description", "amount", "installment", "points"],
        description="Row columns in printed order (cells layout: date, description, amount first)",
    )
    row_pattern: str | None = Field(
        default=None,
        description="line layout: regex with named groups date, description, amount and optional installment, points",
    )
    amount_cell_pattern: str | None = Field(
        default=None, description="Regex for the amount cell; default: a signed amount with decimals in the format"
    )
    points_pattern: str = r"^\d+(?:[.,]\d+)?$"

    @model_validator(mode="after")
    def _layout(self) -> TableSpec:
        if self.layout == "line" and not self.row_pattern:
            raise ValueError("table.row_pattern is required for layout 'line'")
        if self.layout == "cells" and self.columns[:3] != ["date", "description", "amount"]:
            raise ValueError("cells layout must start with date, description, amount")
        if self.row_pattern:
            groups = _compile(self.row_pattern, "table.row_pattern").groupindex
            for g in ("date", "description", "amount"):
                if g not in groups:
                    raise ValueError(f"table.row_pattern needs a named group {g!r}")
        for p in self.skip_patterns:
            _compile(p, "table.skip_patterns")
        return self


class CellRule(_T):
    pattern: str = Field(
        description="Regex on the cell (tried on the printed cell, then on fold(cell)). Named groups: "
        "left + each (remaining count and amount each), k + n (installment k of n)"
    )
    last: bool = Field(default=False, description="A match means this row is the last installment")

    @field_validator("pattern")
    @classmethod
    def _valid(cls, v: str) -> str:
        _compile(v, "installment.cells")
        return v


class InstallmentSpec(_T):
    cells: list[CellRule] = Field(default_factory=list, description="Patterns of the installment column")
    description: list[str] = Field(
        default_factory=list,
        description="Regexes on fold(description) giving the installment number: group k, optional n",
    )
    index_skip_kinds: list[TransactionKind] = Field(default=["tax"])
    stem_remove: list[str] = Field(
        default_factory=list,
        description="Regexes removed, in order, from fold(description) to get the plan's stable stem",
    )

    @model_validator(mode="after")
    def _valid(self) -> InstallmentSpec:
        for p in self.description:
            if "k" not in _compile(p, "installment.description").groupindex:
                raise ValueError(f"installment.description pattern needs a named group 'k': {p!r}")
        for p in self.stem_remove:
            _compile(p, "installment.stem_remove")
        return self


class KindRule(_T):
    match: str | None = Field(default=None, description="Regex searched in fold(description)")
    credit: bool | None = Field(default=None, description="Only rows printed as credit (true) or debit (false)")
    installment: bool | None = Field(default=None, description="Only rows with (true) or without an installment cell")
    kind: TransactionKind
    direction: Direction | None = Field(default=None, description="Force a direction; default: from the sign")
    component: ComponentRole | None = Field(default=None, description="Role inside an installment cash advance")

    @model_validator(mode="after")
    def _some_condition(self) -> KindRule:
        if self.match is None and self.credit is None and self.installment is None:
            raise ValueError("a kind rule needs match, credit or installment")
        if self.match is not None:
            _compile(self.match, "kinds.match")
        return self


class SummarySpec(_T):
    title: str = Field(description="Label that opens the printed summary block")
    layout: Literal["next_line", "inline"] = "next_line"
    separator: str = ":"
    fields: dict[SummaryField, list[str]]

    @model_validator(mode="after")
    def _all_fields(self) -> SummarySpec:
        missing = [f for f in SummaryField.__args__ if f not in self.fields]  # type: ignore[attr-defined]
        if missing:
            raise ValueError("summary.fields must include " + ", ".join(missing))
        return self


class RatesSpec(_T):
    title: str
    pattern: str = Field(
        default=r"^%\s*(?P<monthly>\d+(?:[.,]\d+)?)\s+%\s*(?P<yearly>\d+(?:[.,]\d+)?)$",
        description="Regex on the line after a rate label; named groups monthly and/or yearly",
    )
    keys: dict[str, str] = Field(default_factory=dict, description="Printed label -> key (default: slug of label)")


class Template(_T):
    """One bank product's statement layout."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=TEMPLATE_ID_RE, description="<country>_<bank>_<product>, lower case")
    country: str = Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")
    bank: str = Field(min_length=1)
    product: str = Field(min_length=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$", description="ISO 4217")
    detect: DetectSpec
    amount: AmountSpec
    date_format: str = Field(description="Tokens DD D MM M MMM YYYY YY, e.g. DD.MM.YYYY, MM/DD/YYYY, YYYY-MM-DD")
    header: HeaderSpec
    table: TableSpec
    installment: InstallmentSpec = Field(default_factory=InstallmentSpec)
    foreign_pattern: str | None = Field(
        default=r"\s(?P<amount>\d[\d.,]*)\s+(?P<currency>[A-Z]{3})$",
        description="Regex on the description for a foreign amount: groups amount, currency",
    )
    kinds: list[KindRule] = Field(default_factory=list, description="First matching rule wins")
    default_kind: TransactionKind = "purchase"
    credit_kind: TransactionKind = Field(default="refund", description="Kind of an unmatched credit row")
    merchant_kinds: list[TransactionKind] = Field(default=["purchase", "installment", "refund"])
    summary: SummarySpec | None = None
    rates: RatesSpec | None = None
    password_hint: str | None = Field(
        default=None, max_length=200,
        description="What the bank uses as the PDF password, e.g. 'last 6 digits of your national ID'. "
                    "Shown when an encrypted statement needs one. Never the password itself.",
    )

    @field_validator("date_format")
    @classmethod
    def _date_tokens(cls, v: str) -> str:
        if not re.search(r"D", v) or not re.search(r"M", v):
            raise ValueError("date_format needs day and month tokens")
        return v

    @field_validator("foreign_pattern")
    @classmethod
    def _foreign(cls, v: str | None) -> str | None:
        if v is not None:
            groups = _compile(v, "foreign_pattern").groupindex
            if not {"amount", "currency"} <= set(groups):
                raise ValueError("foreign_pattern needs named groups amount and currency")
        return v


def load_template(path: str | Path) -> Template:
    """Read and validate a template file. Raises ``ValueError`` with a readable message."""
    p = Path(path)
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"{p.name}: not valid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ValueError(f"{p.name}: a template must be a mapping")
    return Template.model_validate(raw)


def template_json_schema() -> dict[str, Any]:
    schema = Template.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = "https://github.com/kalan/kalan-back/templates/template.schema.json"
    return schema


# ------------------------------------------------------------------------------------------ engine


@dataclass
class _Row:
    date: date
    description: str
    amount: int
    sign: str | None
    remaining: tuple[int, int] | None = None  # (count, amount each)
    is_last: bool = False
    cell_index: tuple[int, int] | None = None  # (k, n) printed in the installment column
    points: str | None = None
    extra: list[str] = field(default_factory=list)
    detail: str | None = None  # sub-line printed under the row
    plan_extra: str | None = None  # printed plan identity (purchase date, plan total) added to the plan id
    foreign: tuple[int, str] | None = None  # original amount and currency read from a cell or sub-line


class TemplateParser(StatementParser):
    """A :class:`StatementParser` driven by a :class:`Template`."""

    template_file: ClassVar[str | None] = None
    """Python subclasses may bind a built-in template file by name."""

    def __init__(self, template: Template | None = None, source: Path | None = None) -> None:
        if template is None:
            if self.template_file is None:
                raise TypeError("TemplateParser needs a template")
            source = BUILTIN_DIR / self.template_file
            template = load_template(source)
        self.template = template
        self.source = source
        self.id = template.id
        self.bank = template.bank
        self.product = template.product
        self.country = template.country
        self.currency = template.currency
        self.amount_format = template.amount.format()
        self.date_format = template.date_format
        self.password_hint = template.password_hint

    # ------------------------------------------------------------------------------- compiled

    @cached_property
    def _c(self) -> dict[str, Any]:
        t = self.template
        fmt = self.amount_format
        th = re.escape(fmt.thousands) if fmt.thousands not in ("", " ") else ("[  ]" if fmt.thousands else "")
        whole = rf"\d{{1,3}}(?:{th}\d{{3}})*" if th else r"\d+"
        frac = rf"{re.escape(fmt.decimal)}\d{{{fmt.decimals}}}" if fmt.decimals else ""
        amount_cell = t.table.amount_cell_pattern or rf"^[+-]?{whole}{frac}-?$"
        return {
            "detect_all": [fold(m) for m in t.detect.all],
            "detect_none": [fold(m) for m in t.detect.none],
            "header": {fold(label): key for key, labels in t.header.fields.items() for label in labels},
            "card": re.compile(t.header.card_last4_pattern),
            "table_header": tuple(fold(h) for h in t.table.header),
            "table_end": {fold(e) for e in t.table.end},
            "skip_lines": {fold(s) for s in t.table.skip_lines},
            "skip_patterns": [re.compile(p) for p in t.table.skip_patterns],
            "amount_cell": re.compile(amount_cell),
            "points": re.compile(t.table.points_pattern),
            "row": _compile(t.table.row_pattern, "row_pattern") if t.table.row_pattern else None,
            "cells": [(_compile(c.pattern, "cells"), c.last) for c in t.installment.cells],
            "index": [re.compile(p) for p in t.installment.description],
            "stem": [re.compile(p) for p in t.installment.stem_remove],
            "foreign": re.compile(t.foreign_pattern) if t.foreign_pattern else None,
            "kinds": [(re.compile(k.match) if k.match else None, k) for k in t.kinds],
            "summary": (
                {fold(label): key for key, labels in t.summary.fields.items() for label in labels} if t.summary else {}
            ),
            "rates": re.compile(t.rates.pattern) if t.rates else None,
            "rate_keys": {fold(k): v for k, v in t.rates.keys.items()} if t.rates else {},
        }

    # -------------------------------------------------------------------------------- detect

    def detect(self, text: str) -> bool:
        f = fold(text)
        return all(m in f for m in self._c["detect_all"]) and not any(m in f for m in self._c["detect_none"])

    # --------------------------------------------------------------------------------- parse

    def parse(self, text: str) -> Statement:
        pages = self.pages(text)
        lines = [ln.strip() for ln in text.replace("\r", "").replace("\f", "\n").split("\n")]
        lines = [ln for ln in lines if ln]
        folded = [fold(ln) for ln in lines]

        header = self.header_fields(lines, folded)
        for required in REQUIRED_HEADER:
            if required not in header:
                raise ParseError(f"header field missing: {required}")

        period_end = self._date(header["period_end"])
        rows = self._read_rows(text, lines, folded)
        summary = self._summary(lines, folded)
        rates = self._rates(lines, folded)
        card_last4 = self._card_last4(header.get("card"))

        st_id = self.stable_id("st", self.bank, card_last4, period_end.isoformat())
        transactions = self._transactions(rows, st_id, card_last4, month_key(period_end))

        carry = next((t for t in transactions if t.kind == "carry_over"), None)
        if "period_start" in header:
            period_start = self._date(header["period_start"])
        elif carry is not None:
            period_start = carry.date + timedelta(days=1)
        else:
            prev = add_months(month_key(period_end), -1)
            y, m = (int(p) for p in prev.split("-"))
            period_start = date(y, m, min(period_end.day, 28)) + timedelta(days=1)

        return Statement(
            id=st_id,
            bank=self.bank,
            product=self.product,
            card_last4=card_last4,
            period_start=period_start,
            period_end=period_end,
            due_date=self._date(header["due_date"]),
            currency=self.currency,
            statement_total=self._signed_debt(header["statement_total"]),
            minimum_payment=self._amount(header["minimum_payment"]),
            transactions=transactions,
            interest_rates=rates,
            parser_id=self.id,
            credit_limit=self._amount(header["credit_limit"]) if "credit_limit" in header else None,
            next_period_end=self._date(header["next_period_end"]) if "next_period_end" in header else None,
            next_due_date=self._date(header["next_due_date"]) if "next_due_date" in header else None,
            printed_summary=summary,
            page_count=len(pages),
        )

    # ------------------------------------------------------------------------------ helpers

    def _date(self, value: str) -> date:
        d = try_parse_date(value, self.date_format)
        if d is None:
            raise ParseError(f"bad date: {value!r}")
        return d

    def _amount(self, value: str) -> int:
        try:
            return parse_amount(value, self.amount_format).minor
        except ValueError:
            raise ParseError(f"bad amount: {value!r}") from None

    def _is_credit(self, sign: str | None) -> bool:
        return sign is not None and sign in self.template.amount.credit_signs

    def _signed_debt(self, value: str) -> int:
        """Printed balance as debt: plain = owed (positive), a credit sign = in the holder's favour."""
        try:
            p = parse_amount(value, self.amount_format)
        except ValueError:
            raise ParseError(f"bad amount: {value!r}") from None
        return -p.minor if self._is_credit(p.sign) else p.minor

    def _card_last4(self, value: str | None) -> str | None:
        if not value:
            return None
        m = self._c["card"].search(value.strip())
        return m.group(1) if m else None

    def _labelled(
        self, lines: list[str], folded: list[str], keys: dict[str, str], layout: str, sep: str, start: int = 0
    ) -> dict[str, str]:
        out: dict[str, str] = {}
        sep_f = fold(sep)
        for i in range(start, len(lines)):
            if layout == "inline":
                if sep not in lines[i]:
                    continue
                label, _, value = lines[i].partition(sep)
                key = keys.get(fold(label))
                if key and key not in out and value.strip():
                    out[key] = value.strip()
                continue
            if layout == "colon_line":
                if i + 2 >= len(lines) or folded[i + 1] != sep_f:
                    continue
                key, value = keys.get(folded[i]), lines[i + 2]
            else:
                if i + 1 >= len(lines):
                    continue
                key, value = keys.get(folded[i]), lines[i + 1]
            if key and key not in out:
                out[key] = value
        return out

    def header_fields(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        h = self.template.header
        return self._labelled(lines, folded, self._c["header"], h.layout, h.separator)

    def _table_lines(self, lines: list[str], folded: list[str]) -> list[str]:
        head = self._c["table_header"]
        start = None
        for i in range(len(folded) - len(head) + 1):
            if tuple(folded[i : i + len(head)]) == head:
                start = i + len(head)
                break
        if start is None:
            raise ParseError("transaction table header not found")
        end = next((j for j in range(start, len(folded)) if folded[j] in self._c["table_end"]), None)
        if end is None:
            raise ParseError("end of transaction table not found")
        out = []
        for ln, f in zip(lines[start:end], folded[start:end], strict=True):
            if f in self._c["skip_lines"] or f in head or any(p.match(f) for p in self._c["skip_patterns"]):
                continue
            out.append(ln)
        return out

    def _cell(self, row: _Row, cell: str) -> bool:
        """Apply an installment or points cell to ``row``; False if the cell is not recognised."""
        cols = self.template.table.columns
        if "installment" in cols:
            for pattern, last in self._c["cells"]:
                m = pattern.match(cell) or pattern.match(fold(cell))
                if not m:
                    continue
                g = m.groupdict()
                if last:
                    row.is_last = True
                elif g.get("left") is not None and g.get("each") is not None:
                    row.remaining = (int(g["left"]), self._amount(g["each"]))
                elif g.get("k") is not None and g.get("n") is not None:
                    k, n = int(g["k"]), int(g["n"])
                    if not 1 <= k <= n:
                        return False
                    row.cell_index = (k, n)
                    if k == n:
                        row.is_last = True
                    else:
                        row.remaining = (n - k, row.amount)
                else:
                    return False
                return True
        if "points" in cols and self._c["points"].match(cell) and row.points is None:
            row.points = cell
            return True
        return False

    def _read_rows(self, text: str, lines: list[str], folded: list[str]) -> list[_Row]:
        """Rows of the statement; Python subclasses that need the page breaks override this."""
        del text
        return self._rows(lines, folded)

    def _rows(self, lines: list[str], folded: list[str]) -> list[_Row]:
        table = self._table_lines(lines, folded)
        if self.template.table.layout == "line":
            return self._rows_line(table)
        is_date = re.compile(r"^\S+(?:\s\S+){0,2}$")
        amount_cell = self._c["amount_cell"]

        def date_or_none(s: str) -> date | None:
            return try_parse_date(s, self.date_format) if is_date.match(s) else None

        rows: list[_Row] = []
        i = 0
        while i < len(table):
            row_date = date_or_none(table[i])
            if row_date is None:
                raise ParseError("transaction row does not start with a date")
            i += 1
            desc: list[str] = []
            while i < len(table) and not amount_cell.match(table[i]):
                if date_or_none(table[i]) is not None:
                    raise ParseError("transaction row without amount")
                desc.append(table[i])
                i += 1
            if i >= len(table) or not desc:
                raise ParseError("transaction row without description or amount")
            amount = parse_amount(table[i], self.amount_format)
            i += 1
            row = _Row(date=row_date, description=" ".join(desc), amount=amount.minor, sign=amount.sign)
            while i < len(table) and date_or_none(table[i]) is None:
                if not self._cell(row, table[i]):
                    row.extra.append(table[i])
                i += 1
            if row.extra:
                raise ParseError("unexpected cell after amount")
            rows.append(row)
        return rows

    def _rows_line(self, table: list[str]) -> list[_Row]:
        pattern = self._c["row"]
        rows: list[_Row] = []
        for ln in table:
            m = pattern.match(ln)
            if not m:
                raise ParseError("table line does not match row_pattern")
            g = m.groupdict()
            amount = parse_amount(g["amount"].strip(), self.amount_format)
            row = _Row(
                date=self._date(g["date"].strip()),
                description=re.sub(r"\s+", " ", g["description"]).strip(),
                amount=amount.minor,
                sign=amount.sign,
            )
            for col in ("installment", "points"):
                cell = (g.get(col) or "").strip()
                if cell and not self._cell(row, cell):
                    raise ParseError(f"unrecognised {col} cell")
            rows.append(row)
        return rows

    def _summary(self, lines: list[str], folded: list[str]) -> PrintedSummary | None:
        s = self.template.summary
        if s is None:
            return None
        try:
            start = folded.index(fold(s.title))
        except ValueError:
            return None
        keys: dict[str, str] = self._c["summary"]
        wanted = len(SummaryField.__args__)  # type: ignore[attr-defined]
        raw: dict[str, str] = {}
        if s.layout == "inline":
            raw = self._labelled(lines, folded, keys, "inline", s.separator, start + 1)
        else:
            i = start + 1
            while i < len(lines) - 1 and len(raw) < wanted:
                key = keys.get(folded[i])
                if key and key not in raw and try_parse_amount(lines[i + 1], self.amount_format) is not None:
                    raw[key] = lines[i + 1]
                    i += 2
                    continue
                if key is None and raw:
                    break
                i += 1
        if len(raw) != wanted:
            raise ParseError("summary block incomplete")
        values = {k: self._amount(v) if k == "payments" else self._signed_debt(v) for k, v in raw.items()}
        return PrintedSummary(**values)

    def _rates(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        r = self.template.rates
        rates: dict[str, str] = {}
        if r is None or fold(r.title) not in folded:
            return rates
        start = folded.index(fold(r.title))
        for i in range(start + 1, len(lines) - 1):
            m = self._c["rates"].match(lines[i + 1])
            if not m:
                continue
            key = self._c["rate_keys"].get(folded[i]) or re.sub(r"[^a-z0-9]+", "_", folded[i]).strip("_")
            for part in ("monthly", "yearly"):
                if m.groupdict().get(part) is not None:
                    rates[f"{key}_{part}"] = m.group(part).replace(",", ".")
        return rates

    # ------------------------------------------------------------------------- transactions

    def classify(
        self, description: str, credit: bool, has_cell: bool
    ) -> tuple[TransactionKind, Direction, ComponentRole | None]:
        d = fold(description)
        direction: Direction = "credit" if credit else "debit"
        for pattern, rule in self._c["kinds"]:
            if pattern is not None and not pattern.search(d):
                continue
            if rule.credit is not None and rule.credit != credit:
                continue
            if rule.installment is not None and rule.installment != has_cell:
                continue
            return rule.kind, rule.direction or direction, rule.component
        if credit:
            return self.template.credit_kind, "credit", None
        return self.template.default_kind, direction, None

    def _installment_index(self, description: str) -> tuple[int | None, int | None]:
        d = fold(description)
        for pattern in self._c["index"]:
            m = pattern.search(d)
            if not m:
                continue
            k = int(m.group("k"))
            n = int(m.group("n")) if m.groupdict().get("n") is not None else None
            if n is not None and not 1 <= k <= n:
                continue
            return k, n
        return None, None

    def _stem(self, description: str) -> str:
        foreign = self._c["foreign"]
        d = fold(foreign.sub("", description) if foreign else description)
        for pattern in self._c["stem"]:
            d = pattern.sub(" ", d)
        return re.sub(r"\s+", " ", d).strip(" -/.")

    def _transactions(self, rows: list[_Row], st_id: str, card: str | None, cut_month: str) -> list[Transaction]:
        classified = [
            (row, *self.classify(row.description, self._is_credit(row.sign), row.remaining is not None or row.is_last))
            for row in rows
        ]
        skip_index = set(self.template.installment.index_skip_kinds)

        def index_of(row: _Row, kind: str) -> tuple[int | None, int | None]:
            if row.cell_index is not None:
                return row.cell_index
            return self._installment_index(row.description) if kind not in skip_index else (None, None)

        # Installment cash advances: every component row (principal, BSMV, KKDF, interest) and every
        # installment billed in this statement (early payoff bills the rest at once) joins one plan,
        # anchored at the month of its first installment (lowest installment number seen per stem).
        cash_min_index: dict[str, int] = {}
        for row, kind, _direction, component in classified:
            if component in ("principal", "bsmv", "kkdf", "interest"):
                index, _ = index_of(row, kind)
                stem = self._stem(row.description)
                if index is not None:
                    cash_min_index[stem] = min(index, cash_min_index.get(stem, index))

        out: list[Transaction] = []
        cash_plans_by_date: dict[date, str] = {}
        last_cash_plan: str | None = None
        pending_payoff: list[int] = []
        foreign_re = self._c["foreign"]
        merchant_kinds = set(self.template.merchant_kinds)

        for n, (row, kind, direction, component) in enumerate(classified):
            index, total = index_of(row, kind)
            has_cell = row.remaining is not None or row.is_last
            installment: InstallmentInfo | None = None

            if component == "early_payoff":
                pending_payoff.append(n)
            elif kind == "installment" or (component is not None and (has_cell or index is not None)):
                plan_kind = "cash_advance" if component is not None else "purchase"
                remaining_count, each = row.remaining if row.remaining else (0, 0)
                is_last = row.is_last or row.remaining is None
                if index is not None and total is None:
                    total = index + remaining_count
                stem = self._stem(row.description)
                if plan_kind == "cash_advance" and stem in cash_min_index:
                    anchor = f"start:{add_months(cut_month, -(cash_min_index[stem] - 1))}"
                elif plan_kind == "cash_advance":
                    anchor = f"date:{row.date.isoformat()}"
                elif index is not None:
                    anchor = f"start:{add_months(cut_month, -(index - 1))}:of:{total}"
                else:
                    anchor = f"amount:{row.amount if is_last else each}"
                if row.plan_extra is not None:
                    anchor = f"{anchor}:{row.plan_extra}"
                plan_id = self.stable_id("pl", self.bank, card, plan_kind, stem, anchor)
                installment = InstallmentInfo(
                    remaining_count=remaining_count,
                    remaining_amount_each=each,
                    is_last=is_last,
                    plan_id=plan_id,
                    index=index,
                    total_count=total,
                    component=component,
                )
                if plan_kind == "cash_advance":
                    cash_plans_by_date.setdefault(row.date, plan_id)
                    last_cash_plan = plan_id

            foreign = foreign_re.search(row.description) if foreign_re else None
            original_amount = try_parse_amount(foreign.group("amount"), self.amount_format) if foreign else None
            if original_amount is None:
                foreign = None
            original = (
                row.foreign if row.foreign is not None
                else (original_amount.minor, foreign.group("currency").upper()) if foreign and original_amount
                else None
            )
            names = (
                merchant_fields(row.description) if kind in merchant_kinds and component is None
                else {"merchant": None, "merchant_unreadable": False}
            )
            out.append(
                Transaction(
                    id=self.stable_id("tx", st_id, n),
                    date=row.date,
                    description=row.description,
                    amount=row.amount,
                    currency=self.currency,
                    direction=direction,
                    kind=kind,
                    installment=installment,
                    **names,  # type: ignore[arg-type]
                    source="statement",
                    original_amount=original[0] if original else None,
                    original_currency=original[1] if original else None,
                    points=row.points,
                    detail=row.detail,
                )
            )

        # Early payoff closes the cash advance plan billed on the same day, or the last one in the statement.
        for pos in pending_payoff:
            tx = out[pos]
            plan_id = cash_plans_by_date.get(tx.date, last_cash_plan)
            if plan_id is None:
                continue
            out[pos] = tx.model_copy(
                update={
                    "installment": InstallmentInfo(
                        remaining_count=0,
                        remaining_amount_each=0,
                        is_last=True,
                        plan_id=plan_id,
                        component="early_payoff",
                    )
                }
            )
        return out
