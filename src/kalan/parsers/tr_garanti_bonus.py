"""Garanti BBVA Bonus credit card statement (Turkey, TRY).

A Python parser on top of the template engine: ``src/kalan/templates/tr_garanti_bonus.yaml`` holds
detection, formats, header labels and kind rules; this module reads the parts a template cannot
describe. Layout learned from redacted real statements; ``tools/synth_garanti.py`` reproduces it.

Why not a plain template:

* rows are grouped under section titles (bonus partners, other spending, foreign spending) and
  sector titles (``Süpermarket``); those lines sit between rows and are not rows;
* the Bonus column is printed before the amount, so a row may end with two amounts: bonus, then amount;
* the installment cell (``<each>x<n>=<plan total> <k>.Taksit``) and a foreign amount cell
  (``1,23 USD``) come before the amounts;
* credits carry a trailing sign (``1.234,56+``, ``12,34-``);
* every continuation page repeats a page header (card, due date, total) before the table header;
* the opening balance row has no date; the previous and next cut-off dates are printed only in the
  sentence under ``EKSTRE ÖZETİ``;
* the summary prints its labels first and the values afterwards, with the formula's ``+ - =`` signs.
"""

from __future__ import annotations

import re
from calendar import monthrange
from datetime import date, timedelta

from kalan.amounts import add_months, month_key, try_parse_amount, try_parse_date
from kalan.models import PrintedSummary
from kalan.parsers.base import ParseError
from kalan.parsers.template import TemplateParser, _Row
from kalan.text import fold

_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
_AMOUNT_CELL = re.compile(rf"^(?:[+-]{_AMOUNT}|{_AMOUNT}[+-]?)$")
_FOREIGN_CELL = re.compile(rf"^(?P<amount>{_AMOUNT})\s+(?P<currency>TL|[A-Z]{{3}})$")
_INSTALLMENT_CELL = re.compile(
    rf"^(?P<each>{_AMOUNT})\s*x\s*(?P<n>\d{{1,2}})\s*=\s*(?P<total>{_AMOUNT})\s+(?P<k>\d{{1,2}})\s*\.\s*taksit$",
    re.IGNORECASE,
)
_CARRY = "onceki donemden devir edilen tutar"
_TOTAL = "toplam"
_BONUS_ONLY = re.compile(r"^bonus .*(?:alisveris|kullanim)")  # rows printed only in the Bonus column
_DATE_WORDS = r"\d{1,2} [a-z]+ \d{4}"
_NEXT = re.compile(rf"bir sonraki hesap kesiminiz ({_DATE_WORDS}) \w+ ve son odemeniz ({_DATE_WORDS})")
_PREVIOUS = re.compile(rf"bir onceki hesap kesim tarihiniz ({_DATE_WORDS})")
_SUMMARY_LABELS = (
    "onceki bakiye",
    "donem harcamalari",
    "faiz ve ucretler",
    "odemeleriniz",
    "donem borcunuz",
    "min. odeme tutariniz",
)
_RATE_NAMES = (  # longest first: "nakit avans gecikme faizi" before "gecikme faizi"
    ("nakit avans gecikme faizi", "cash_advance_late"),
    ("nakit avans faizi", "cash_advance"),
    ("limit asim faizi", "over_limit"),
    ("gecikme faizi", "purchase_late"),
    ("akdi faiz", "purchase"),
)
_RATE = re.compile(
    "(" + "|".join(re.escape(n) for n, _ in _RATE_NAMES) + r")\s*:\s*%\s*(\d+(?:,\d+)?)"
)
_MONTHS_TR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
              "Aralık")


def _tr_date(d: date) -> str:
    return f"{d.day} {_MONTHS_TR[d.month - 1]} {d.year}"


class GarantiBonus(TemplateParser):
    template_file = "tr_garanti_bonus.yaml"

    # ------------------------------------------------------------------------------ header

    def _summary_sentence(self, folded: list[str]) -> str:
        try:
            start = folded.index("ekstre ozeti")
        except ValueError:
            return ""
        return " ".join(folded[start + 1 : start + 4])

    def header_fields(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        out = super().header_fields(lines, folded)
        sentence = self._summary_sentence(folded)
        m = _NEXT.search(sentence)
        if m:
            out["next_period_end"], out["next_due_date"] = m.group(1), m.group(2)
        previous = self._previous_cut(folded, out)
        if previous is not None:
            out["period_start"] = _tr_date(previous + timedelta(days=1))
        return out

    def _previous_cut(self, folded: list[str], header: dict[str, str]) -> date | None:
        m = _PREVIOUS.search(self._summary_sentence(folded))
        if m:
            return try_parse_date(m.group(1), self.date_format)
        if "period_end" not in header:
            return None
        end = try_parse_date(header["period_end"], self.date_format)
        if end is None:
            return None
        y, mo = (int(p) for p in add_months(month_key(end), -1).split("-"))
        return date(y, mo, min(end.day, monthrange(y, mo)[1]))

    # ------------------------------------------------------------------------------- table

    def _read_rows(self, text: str, lines: list[str], folded: list[str]) -> list[_Row]:
        header = self.header_fields(lines, folded)
        if "period_start" not in header:
            raise ParseError("header field missing: period_end")
        carry_date = self._date(header["period_start"]) - timedelta(days=1)
        pages = [[ln.strip() for ln in page.replace("\r", "").split("\n") if ln.strip()] for page in self.pages(text)]
        return self._table_rows(self._table_body(pages), carry_date)

    def _table_body(self, pages: list[list[str]]) -> list[tuple[str, str]]:
        """Table lines from the first table header to ``Toplam``, without page headers and fillers."""
        head = self._c["table_header"]
        skip_lines = self._c["skip_lines"]
        skip_patterns = self._c["skip_patterns"]
        out: list[tuple[str, str]] = []
        in_table = False
        for page in pages:
            pf = [fold(ln) for ln in page]
            pos = next((i for i in range(len(pf) - len(head) + 1) if tuple(pf[i : i + len(head)]) == head), None)
            if pos is None:
                if in_table:
                    raise ParseError("continuation page without table header")
                continue
            in_table = True
            for ln, f in zip(page[pos + len(head) :], pf[pos + len(head) :], strict=True):
                if f == _TOTAL:
                    return out
                if f in skip_lines or any(p.match(f) for p in skip_patterns):
                    continue
                out.append((ln, f))
        if not in_table:
            raise ParseError("transaction table header not found")
        raise ParseError("end of transaction table not found")

    def _is_date(self, line: str) -> date | None:
        return try_parse_date(line, self.date_format) if len(line) <= 20 else None

    def _is_cell(self, line: str) -> bool:
        return bool(_AMOUNT_CELL.match(line) or _FOREIGN_CELL.match(line) or _INSTALLMENT_CELL.match(line))

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
            if row_date is None:  # section or sector title between rows
                i += 1
                continue
            i += 1
            desc: list[str] = []
            while i < len(body) and not self._is_cell(body[i][0]):
                if self._is_date(body[i][0]) is not None:
                    raise ParseError("transaction row without amount")
                desc.append(body[i][0])
                i += 1
            cells: list[str] = []
            while i < len(body) and self._is_cell(body[i][0]):
                cells.append(body[i][0])
                i += 1
            if not desc:
                raise ParseError("transaction row without description")
            rows.append(self._row(row_date, " ".join(desc), cells))
        return rows

    def _parsed(self, text: str):
        p = try_parse_amount(text, self.amount_format)
        if p is None:
            raise ParseError("bad amount cell")
        return p

    def _row(self, row_date: date, description: str, cells: list[str]) -> _Row:
        amounts = [c for c in cells if _AMOUNT_CELL.match(c)]
        others = [c for c in cells if not _AMOUNT_CELL.match(c)]
        if not amounts or len(amounts) > 2 or cells[-1] != amounts[-1]:
            raise ParseError("transaction row without a single amount at the end")
        amount = self._parsed(amounts[-1])
        if len(amounts) == 1 and _BONUS_ONLY.search(fold(description)):
            # Bonus spent: the value sits in the Bonus column, the row adds nothing to the TL total.
            return _Row(date=row_date, description=description, amount=0, sign=amount.sign, points=amounts[0])
        row = _Row(date=row_date, description=description, amount=amount.minor, sign=amount.sign)
        if len(amounts) == 2:
            row.points = amounts[0]
        for cell in others:
            m = _INSTALLMENT_CELL.match(cell)
            if m:
                k, n = int(m.group("k")), int(m.group("n"))
                if not 1 <= k <= n:
                    raise ParseError("bad installment cell")
                row.cell_index = (k, n)
                row.plan_extra = f"total:{self._amount(m.group('total'))}"  # the row date moves every month
                if k == n:
                    row.is_last = True
                else:
                    row.remaining = (n - k, self._amount(m.group("each")))
                continue
            m = _FOREIGN_CELL.match(cell)
            if m and m.group("currency") != "TL":
                row.foreign = (self._amount(m.group("amount")), m.group("currency"))
        return row

    # ----------------------------------------------------------------------------- summary

    def _summary(self, lines: list[str], folded: list[str]) -> PrintedSummary | None:
        try:
            start = folded.index("ekstre ozeti")
        except ValueError:
            return None
        n = len(_SUMMARY_LABELS)
        at = next(
            (i for i in range(start, len(folded) - n + 1) if tuple(folded[i : i + n]) == _SUMMARY_LABELS), None
        )
        if at is None:
            raise ParseError("summary block incomplete")
        values: list[str] = []
        for ln in lines[at + n :]:
            if ln in ("+", "-", "="):
                continue
            v = re.sub(r"\s*[+=]$", "", ln).strip()
            if try_parse_amount(v, self.amount_format) is None:
                break
            values.append(v)
            if len(values) == n:
                break
        if len(values) != n:
            raise ParseError("summary block incomplete")
        previous, period, fees, payments, total, _minimum = values
        return PrintedSummary(
            previous_balance=self._signed_debt(previous),
            period_transactions=self._signed_debt(period),
            interest_and_fees=self._signed_debt(fees),
            payments=self._amount(payments),
            statement_total=self._signed_debt(total),
        )

    def _rates(self, lines: list[str], folded: list[str]) -> dict[str, str]:
        """``Aylık güncel faiz oranları: Akdi faiz: %3,75 , ...`` and the same line for yearly rates."""
        try:
            start = next(i for i, f in enumerate(folded) if f.startswith("aylik guncel faiz oranlari"))
        except StopIteration:
            return {}
        block: list[str] = []
        for f in folded[start:]:
            if f in ("bosluk",) or (block and not f.startswith("yillik") and len(block) >= 4):
                break
            block.append(f)
        joined = " ".join(block)
        monthly, _, yearly = joined.partition("yillik guncel faiz oranlari")
        names = dict(_RATE_NAMES)
        rates: dict[str, str] = {}
        for part, text in (("monthly", monthly), ("yearly", yearly)):
            for m in _RATE.finditer(text):
                rates.setdefault(f"{names[m.group(1)]}_{part}", m.group(2).replace(",", "."))
        return rates
