"""Yapı Kredi World credit card statement (Turkey, TRY).

A Python parser on top of the template engine: ``src/kalan/templates/tr_yapikredi_world.yaml`` holds
detection, formats, header labels and kind rules; this module reads the parts a template cannot
describe. Layout learned from redacted real statements; ``tools/synth_yapikredi.py`` reproduces it.

Why not a plain template:

* header values sit on the line after their label with a leading colon (``Dönem Borcu`` / ``: 1.234,56 TL``);
* one statement holds one table per card (plastic and digital card), each with its own header and
  ``TOPLAM`` row;
* the opening balance row has no date;
* a row may carry sub-lines after its cells: ``ABNO ... FTNO ...`` on bill payments,
  ``<total> TL'lik işlemin k / n taksidi`` on installments, ``İşlem Tutarı: ...`` on card-not-present
  and foreign charges. They are attached to the row as ``detail`` and never become rows;
* the installment cell prints what is left of the plan as ``<remaining total> / <remaining count>``;
* the printed summary has no separate interest and fee line: ``Dönem İçi Harcamalar`` includes them.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date

from kalan.amounts import add_months, month_key, try_parse_amount, try_parse_date
from kalan.models import PrintedSummary
from kalan.parsers.base import ParseError
from kalan.parsers.template import TemplateParser, _Row

_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
_AMOUNT_CELL = re.compile(rf"^[+-]?{_AMOUNT}$")
_REMAINING_CELL = re.compile(rf"^(?P<total>{_AMOUNT})\s*/\s*(?P<count>\d{{1,2}})$")
_PLAN_LINE = re.compile(
    rf"^(?P<total>{_AMOUNT})\s*tl.?lik islemin\s+(?P<k>\d{{1,2}})\s*/\s*(?P<n>\d{{1,2}})\s+taksidi$"
)
_FOREIGN_LINE = re.compile(rf"^islem tutari\s*:\s*(?P<amount>{_AMOUNT})\s+(?P<currency>[a-z]{{3}})\b")
_CARRY = "onceki donem hesap ozeti borcu"
_TABLE_END = ("toplam", "puan ozeti", "dijital kart numarasi")
# Page footer lines, dropped if a table runs over a page break.
_FOOTER = (
    re.compile(r"^temassiz islem$"),
    re.compile(r"^yapi ve kredi bankasi a\.s\.$"),
    re.compile(r"^buyuk mukellefler v\.d\."),
    re.compile(r"^sira no:"),
    re.compile(r"^\d+ / \d+(?:\s+\d+)?$"),
)
_SUMMARY = {
    "onceki donem hesap ozeti borcu": "previous_balance",
    "donem ici harcamalar": "period_transactions",
    "donem ici odemeler": "payments",
    "donem borcu": "statement_total",
    "kalan toplam taksit tutari": "remaining_installments",
}
_RATE_ROWS = {"akdi faiz orani": "", "gecikme faiz orani": "_late"}
_RATE_CELL = re.compile(r"^%\s*(?P<monthly>\d+(?:,\d+)?)\s*/\s*%\s*(?P<yearly>\d+(?:,\d+)?)$")


def _colon_value(value: str) -> str:
    v = value.strip()
    return v[1:].strip() if v.startswith(":") else v


class YapikrediWorld(TemplateParser):
    template_file = "tr_yapikredi_world.yaml"

    # ------------------------------------------------------------------------------ header

    def header_fields(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        keys: dict[str, str] = self._c["header"]
        for i in range(len(lines) - 1):
            key = keys.get(folded[i])
            if key is None or key in out or not lines[i + 1].startswith(":"):
                continue
            value = _colon_value(lines[i + 1])
            if key == "minimum_payment":
                value = value.split("/")[0].strip()
            out[key] = value
        return out

    def _previous_cut(self, lines: list[str], folded: list[str]) -> date:
        """The statement does not print the previous cut-off date: one month before this one."""
        header = self.header_fields(lines, folded)
        if "period_end" not in header:
            raise ParseError("header field missing: period_end")
        end = self._date(header["period_end"])
        y, m = (int(p) for p in add_months(month_key(end), -1).split("-"))
        return date(y, m, min(end.day, monthrange(y, m)[1]))

    # ------------------------------------------------------------------------------- table

    def _rows(self, lines: list[str], folded: list[str]) -> list[_Row]:
        head = self._c["table_header"]
        starts = [i + len(head) for i in range(len(folded) - len(head) + 1) if tuple(folded[i : i + len(head)]) == head]
        if not starts:
            raise ParseError("transaction table header not found")
        carry_date = self._previous_cut(lines, folded)
        rows: list[_Row] = []
        end = -1
        for start in starts:
            if start <= end:  # a repeated header inside a table that runs over a page break
                continue
            # A table ends at its TOPLAM row; a table holding only the opening balance has none.
            end = next((j for j in range(start, len(folded)) if folded[j] in _TABLE_END), -1)
            if end < 0:
                raise ParseError("end of transaction table not found")
            body = [
                (ln, f)
                for ln, f in zip(lines[start:end], folded[start:end], strict=True)
                if f not in head and not any(p.match(f) for p in _FOOTER)
            ]
            rows.extend(self._table_rows(body, carry_date))
        return rows

    def _is_date(self, line: str) -> date | None:
        return try_parse_date(line, self.date_format) if len(line) <= 20 else None

    def _table_rows(self, body: list[tuple[str, str]], carry_date: date) -> list[_Row]:
        rows: list[_Row] = []
        i = 0
        while i < len(body):
            line, f = body[i]
            if f == _CARRY:
                if i + 1 >= len(body) or not _AMOUNT_CELL.match(body[i + 1][0]):
                    raise ParseError("opening balance row without amount")
                amount = self._parsed(body[i + 1][0])
                rows.append(_Row(date=carry_date, description=line, amount=amount.minor, sign=amount.sign))
                i += 2
                continue
            row_date = self._is_date(line)
            if row_date is None:
                raise ParseError("transaction row does not start with a date")
            i += 1
            desc: list[str] = []
            while i < len(body) and not _AMOUNT_CELL.match(body[i][0]):
                if self._is_date(body[i][0]) is not None or body[i][1] == _CARRY:
                    raise ParseError("transaction row without amount")
                desc.append(body[i][0])
                i += 1
            if i >= len(body) or not desc:
                raise ParseError("transaction row without description or amount")
            amount = self._parsed(body[i][0])
            i += 1
            row = _Row(date=row_date, description=" ".join(desc), amount=amount.minor, sign=amount.sign)
            details: list[str] = []
            while i < len(body) and self._is_date(body[i][0]) is None and body[i][1] != _CARRY:
                self._cell_or_detail(row, body[i][0], body[i][1], details)
                i += 1
            row.detail = " | ".join(details) or None
            self._finish_installment(row)
            rows.append(row)
        return rows

    def _parsed(self, text: str):
        p = try_parse_amount(text, self.amount_format)
        if p is None:
            raise ParseError("bad amount cell")
        return p

    def _cell_or_detail(self, row: _Row, line: str, f: str, details: list[str]) -> None:
        m = _REMAINING_CELL.match(line)
        if m and row.remaining is None and not details:
            count = int(m.group("count"))
            total = self._amount(m.group("total"))
            if count < 1:
                raise ParseError("bad installment cell")
            row.remaining = (count, (total + count // 2) // count)
            return
        if self._c["points"].match(line) and row.points is None and not details:
            row.points = line
            return
        m = _PLAN_LINE.match(f)
        if m:
            k, n = int(m.group("k")), int(m.group("n"))
            if not 1 <= k <= n:
                raise ParseError("bad installment line")
            row.cell_index = (k, n)
            row.plan_extra = f"{row.date.isoformat()}:{self._amount(m.group('total'))}"
        m = _FOREIGN_LINE.match(f)
        if m and m.group("currency") not in ("try", "tl"):
            row.foreign = (self._amount(m.group("amount")), m.group("currency").upper())
        details.append(re.sub(r"\s+", " ", line).strip())

    def _finish_installment(self, row: _Row) -> None:
        if row.cell_index is None:
            if row.remaining is not None:
                raise ParseError("installment cell without installment line")
            return
        k, n = row.cell_index
        if k == n:
            if row.remaining is not None:
                raise ParseError("last installment with a remaining cell")
            row.is_last = True
        elif row.remaining is None:
            raise ParseError("installment without remaining cell")

    # ----------------------------------------------------------------------------- summary

    def _summary(self, lines: list[str], folded: list[str]) -> PrintedSummary | None:
        raw: dict[str, str] = {}
        for i in range(len(lines) - 1):
            key = _SUMMARY.get(folded[i])
            if key and key not in raw and lines[i + 1].startswith(":"):
                raw[key] = _colon_value(lines[i + 1])
        if not raw:
            return None
        if len(raw) != len(_SUMMARY):
            raise ParseError("summary block incomplete")
        return PrintedSummary(
            previous_balance=self._signed_debt(raw["previous_balance"]),
            period_transactions=self._signed_debt(raw["period_transactions"]),
            interest_and_fees=None,
            payments=self._amount(raw["payments"]),
            statement_total=self._signed_debt(raw["statement_total"]),
            remaining_installments=self._amount(raw["remaining_installments"]),
        )

    def _rates(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        """``Akdi Faiz Oranı`` / ``Gecikme Faiz Oranı``, each followed by a purchase and a cash cell."""
        rates: dict[str, str] = {}
        for i, f in enumerate(folded):
            suffix = _RATE_ROWS.get(f)
            if suffix is None or i + 2 >= len(lines):
                continue
            for kind, cell in (("purchase", lines[i + 1]), ("cash_advance", lines[i + 2])):
                m = _RATE_CELL.match(cell.replace(" ", ""))
                if m:
                    rates[f"{kind}{suffix}_monthly"] = m.group("monthly").replace(",", ".")
                    rates[f"{kind}{suffix}_yearly"] = m.group("yearly").replace(",", ".")
        return rates
