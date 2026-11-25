"""VakıfBank Worldcard credit card statement (Turkey, TRY).

Layout of ``page.get_text()`` output (learned from redacted real statements):

* Header block ``HESAP BİLGİLERİNİZ``: key, ``:`` and value on three separate lines
  (``Dönem Borcunuz`` / ``:`` / ``2,345.60 TL``).
* Transaction table after the header lines ``İŞLEM TARİHİ``, ``AÇIKLAMA``, ``TUTAR (TL)``,
  ``KALAN TAKSİT``, ``WORLDPUAN``. Each row is consecutive lines: date, description, amount, then an
  optional remaining-installment cell (``2x1,250.40`` or ``Son Taksit``) and an optional points cell.
* Summary block ``HESAP ÖZETİ`` with label and value on alternating lines.
* ``FAİZ ORANLARI``: a label line followed by ``%monthly    %yearly``.
* Footer ``Sayfa 1 / 2``; the table can continue on the next page.

Amounts use comma thousands and dot decimals; credits carry a leading ``+``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from kalan.amounts import DOT_DECIMAL, add_months, month_key, parse_amount, try_parse_amount, try_parse_date
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
from kalan.text import fold, normalize_merchant

_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
_AMOUNT_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})*\.\d{2}$")
_REMAINING_RE = re.compile(r"^(\d{1,3})\s*[xX]\s*(\d{1,3}(?:,\d{3})*\.\d{2})$")
_POINTS_RE = re.compile(r"^\d+(?:[.,]\d+)?$")
_PAGE_FOOTER_RE = re.compile(r"^sayfa \d+ / \d+$")
_RATE_RE = re.compile(r"^%\s*(\d+(?:[.,]\d+)?)\s+%\s*(\d+(?:[.,]\d+)?)$")
_CARD_LAST4_RE = re.compile(r"(?:\*{2,}|x{2,}|X{2,}|\*\*\*\*\s?)(\d{4})\]?$")
_FOREIGN_RE = re.compile(r"\s(\d{1,3}(?:,\d{3})*\.\d{2})\s+(USD|EUR|GBP|CHF)$")
_SLASH_INDEX_RE = re.compile(r"\b(\d{1,2})\s*/\s*(\d{1,2})\b")
_DOT_INDEX_RE = re.compile(r"\b(\d{1,2})\s*\.\s*(?:taksit|bsmv|kkdf|vade)")

_TABLE_HEADER = ("islem tarihi", "aciklama", "tutar (tl)", "kalan taksit", "worldpuan")
_PAGE_TITLE = "kredi karti hesap ozeti (tl)"
_SUMMARY_TITLE = "hesap ozeti"
_RATES_TITLE = "faiz oranlari"

_HEADER_KEYS = {
    "donem borcunuz": "statement_total",
    "asgari odeme tutari": "minimum_payment",
    "son odeme tarihi": "due_date",
    "limitiniz": "credit_limit",
    "hesap kesim tarihi": "period_end",
    "bir sonraki hesap kesim tarihi": "next_period_end",
    "bir sonraki son odeme tarihi": "next_due_date",
    "kart no": "card",
}
_SUMMARY_KEYS = {
    "onceki hesap bakiyesi": "previous_balance",
    "donem ici islemler": "period_transactions",
    "toplam faiz ve ucretler": "interest_and_fees",
    "odemeler": "payments",
    "donem borcunuz": "statement_total",
}
_RATE_KEYS = {
    "alisveris faiz orani": "purchase",
    "nakit cekim faiz orani": "cash_advance",
    "alisveris gecikme faiz orani": "purchase_late",
    "nakit gecikme faiz orani": "cash_advance_late",
}


@dataclass
class _Row:
    date: date
    description: str
    amount: int
    sign: str | None
    remaining: tuple[int, int] | None = None  # (count, amount each)
    is_last: bool = False
    points: str | None = None
    extra: list[str] = field(default_factory=list)


def _signed_debt(text: str) -> int:
    """Printed balance as debt: plain = owed (positive), ``+`` or ``-`` = in the holder's favour."""
    p = parse_amount(text, DOT_DECIMAL)
    return -p.minor if p.sign in ("+", "-") else p.minor


class VakifbankWorldcard(StatementParser):
    id = "tr_vakifbank_worldcard"
    bank = "vakifbank"
    product = "Worldcard"
    country = "TR"
    currency = "TRY"
    amount_format = DOT_DECIMAL
    date_format = "DD.MM.YYYY"

    # ------------------------------------------------------------------------------------- detect

    def detect(self, text: str) -> bool:
        f = fold(text)
        return all(
            marker in f
            for marker in ("vakifbank", "worldcard", "kredi karti hesap ozeti", "islem tarihi", "worldpuan")
        )

    # -------------------------------------------------------------------------------------- parse

    def parse(self, text: str) -> Statement:
        pages = self.pages(text)
        lines = [ln.strip() for ln in text.replace("\r", "").replace("\f", "\n").split("\n")]
        lines = [ln for ln in lines if ln]
        folded = [fold(ln) for ln in lines]

        header = self._header(lines, folded)
        for required in ("statement_total", "minimum_payment", "due_date", "period_end"):
            if required not in header:
                raise ParseError(f"header field missing: {required}")

        period_end = self._date(header["period_end"])
        rows = self._rows(lines, folded)
        summary = self._summary(lines, folded)
        rates = self._rates(lines, folded)
        card_last4 = self._card_last4(header.get("card"))

        st_id = self.stable_id("st", self.bank, card_last4, period_end.isoformat())
        transactions = self._transactions(rows, st_id, card_last4, month_key(period_end))

        carry = next((t for t in transactions if t.kind == "carry_over"), None)
        if carry is not None:
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
            statement_total=_signed_debt(header["statement_total"].removesuffix("TL").strip()),
            minimum_payment=parse_amount(header["minimum_payment"], DOT_DECIMAL).minor,
            transactions=transactions,
            interest_rates=rates,
            parser_id=self.id,
            credit_limit=parse_amount(header["credit_limit"], DOT_DECIMAL).minor if "credit_limit" in header else None,
            next_period_end=self._date(header["next_period_end"]) if "next_period_end" in header else None,
            next_due_date=self._date(header["next_due_date"]) if "next_due_date" in header else None,
            printed_summary=summary,
            page_count=len(pages),
        )

    # ----------------------------------------------------------------------------------- sections

    @staticmethod
    def _date(value: str) -> date:
        d = try_parse_date(value, "DD.MM.YYYY")
        if d is None:
            raise ParseError(f"bad date: {value!r}")
        return d

    @staticmethod
    def _header(lines: list[str], folded: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for i in range(len(lines) - 2):
            if lines[i + 1] != ":":
                continue
            key = _HEADER_KEYS.get(folded[i])
            if key and key not in out:
                out[key] = lines[i + 2]
        return out

    @staticmethod
    def _card_last4(value: str | None) -> str | None:
        if not value:
            return None
        m = _CARD_LAST4_RE.search(value.strip())
        return m.group(1) if m else None

    @staticmethod
    def _table_lines(lines: list[str], folded: list[str]) -> list[str]:
        start = None
        for i in range(len(folded) - len(_TABLE_HEADER) + 1):
            if tuple(folded[i : i + len(_TABLE_HEADER)]) == _TABLE_HEADER:
                start = i + len(_TABLE_HEADER)
                break
        if start is None:
            raise ParseError("transaction table header not found")
        try:
            end = folded.index(_SUMMARY_TITLE, start)
        except ValueError:
            raise ParseError("summary block HESAP ÖZETİ not found") from None
        out = []
        for ln, f in zip(lines[start:end], folded[start:end], strict=True):
            if f == _PAGE_TITLE or f in _TABLE_HEADER or _PAGE_FOOTER_RE.match(f):
                continue
            out.append(ln)
        return out

    def _rows(self, lines: list[str], folded: list[str]) -> list[_Row]:
        table = self._table_lines(lines, folded)
        rows: list[_Row] = []
        i = 0
        while i < len(table):
            if not _DATE_RE.match(table[i]):
                raise ParseError("transaction row does not start with a date")
            row_date = self._date(table[i])
            i += 1
            desc: list[str] = []
            while i < len(table) and not _AMOUNT_RE.match(table[i]):
                if _DATE_RE.match(table[i]):
                    raise ParseError("transaction row without amount")
                desc.append(table[i])
                i += 1
            if i >= len(table) or not desc:
                raise ParseError("transaction row without description or amount")
            amount = parse_amount(table[i], DOT_DECIMAL)
            i += 1
            row = _Row(date=row_date, description=" ".join(desc), amount=amount.minor, sign=amount.sign)
            while i < len(table) and not _DATE_RE.match(table[i]):
                cell = table[i]
                m = _REMAINING_RE.match(cell)
                if m:
                    row.remaining = (int(m.group(1)), parse_amount(m.group(2), DOT_DECIMAL).minor)
                elif fold(cell) == "son taksit":
                    row.is_last = True
                elif _POINTS_RE.match(cell) and row.points is None:
                    row.points = cell
                else:
                    row.extra.append(cell)
                i += 1
            if row.extra:
                raise ParseError("unexpected cell after amount")
            rows.append(row)
        return rows

    @staticmethod
    def _summary(lines: list[str], folded: list[str]) -> PrintedSummary | None:
        try:
            start = folded.index(_SUMMARY_TITLE)
        except ValueError:
            return None
        values: dict[str, int] = {}
        i = start + 1
        while i < len(lines) - 1 and len(values) < len(_SUMMARY_KEYS):
            key = _SUMMARY_KEYS.get(folded[i])
            if key and key not in values and try_parse_amount(lines[i + 1], DOT_DECIMAL) is not None:
                raw = lines[i + 1]
                values[key] = parse_amount(raw, DOT_DECIMAL).minor if key == "payments" else _signed_debt(raw)
                i += 2
                continue
            if key is None and values:
                break
            i += 1
        if len(values) != len(_SUMMARY_KEYS):
            raise ParseError("summary block incomplete")
        return PrintedSummary(**values)

    @staticmethod
    def _rates(lines: list[str], folded: list[str]) -> dict[str, str]:
        rates: dict[str, str] = {}
        if _RATES_TITLE not in folded:
            return rates
        start = folded.index(_RATES_TITLE)
        for i in range(start + 1, len(lines) - 1):
            m = _RATE_RE.match(lines[i + 1])
            if not m:
                continue
            key = _RATE_KEYS.get(folded[i]) or re.sub(r"[^a-z0-9]+", "_", folded[i]).strip("_")
            rates[f"{key}_monthly"] = m.group(1).replace(",", ".")
            rates[f"{key}_yearly"] = m.group(2).replace(",", ".")
        return rates

    # ------------------------------------------------------------------------------ transactions

    @staticmethod
    def _classify(row: _Row) -> tuple[TransactionKind, Direction, ComponentRole | None]:
        d = fold(row.description)
        credit = row.sign in ("+", "-")
        direction: Direction = "credit" if credit else "debit"
        if "onceki donem hesap ozeti bakiyeniz" in d:
            return "carry_over", direction, None
        if "odemeniz icin tesekkurler" in d:
            return "payment", "credit", None
        if "erken kapama" in d and "iptal" in d:
            return "refund", "credit", "early_payoff"
        if "nakit avans" in d or "nakit cekim" in d:
            if "taksit anap" in d or "anapara" in d:
                return "cash_advance", direction, "principal"
            if "bsmv" in d:
                return "fee", direction, "bsmv"
            if "kkdf" in d:
                return "fee", direction, "kkdf"
            if "vade fark" in d:
                return "interest", direction, "interest"
            return "cash_advance", direction, None
        if d.startswith("vergi"):
            return "tax", direction, None
        if credit:
            return "refund", "credit", None
        if "faiz" in d:
            return "interest", direction, None
        if any(k in d for k in ("aidat", "ucret", "komisyon", "masraf", "bsmv", "kkdf")):
            return "fee", direction, None
        if row.remaining is not None or row.is_last:
            return "installment", direction, None
        return "purchase", direction, None

    @staticmethod
    def _installment_index(description: str) -> tuple[int | None, int | None]:
        d = fold(description)
        m = _SLASH_INDEX_RE.search(d)
        if m and 1 <= int(m.group(1)) <= int(m.group(2)):
            return int(m.group(1)), int(m.group(2))
        m = _DOT_INDEX_RE.search(d)
        if m:
            return int(m.group(1)), None
        return None, None

    @staticmethod
    def _stem(description: str) -> str:
        d = fold(_FOREIGN_RE.sub("", description))
        d = _SLASH_INDEX_RE.sub(" ", d)
        d = re.sub(r"\b\d{1,2}\s*\.\s*(?=taksit|bsmv|kkdf|vade)", " ", d)
        d = re.sub(r"\b(taksit anap|anapara|bsmv|kkdf|vade farki|taksit)\b", " ", d)
        return re.sub(r"\s+", " ", d).strip(" -/.")

    def _transactions(self, rows: list[_Row], st_id: str, card: str | None, cut_month: str) -> list[Transaction]:
        classified = [(row, *self._classify(row)) for row in rows]

        # Installment cash advances: every component row (principal, BSMV, KKDF, vade farkı) and every
        # installment billed in this statement (early payoff bills the rest at once) joins one plan.
        # The plan is anchored at the month of its first installment, derived from the lowest
        # installment number seen for that description stem.
        cash_min_index: dict[str, int] = {}
        for row, _kind, _direction, component in classified:
            if component in ("principal", "bsmv", "kkdf", "interest"):
                index, _ = self._installment_index(row.description)
                stem = self._stem(row.description)
                if index is not None:
                    cash_min_index[stem] = min(index, cash_min_index.get(stem, index))

        out: list[Transaction] = []
        cash_plans_by_date: dict[date, str] = {}
        last_cash_plan: str | None = None
        pending_payoff: list[int] = []

        for n, (row, kind, direction, component) in enumerate(classified):
            index, total = self._installment_index(row.description) if kind != "tax" else (None, None)
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

            foreign = _FOREIGN_RE.search(row.description)
            merchant = (
                normalize_merchant(row.description)
                if kind in ("purchase", "installment", "refund") and component is None
                else None
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
                    merchant=merchant,
                    source="statement",
                    original_amount=parse_amount(foreign.group(1), DOT_DECIMAL).minor if foreign else None,
                    original_currency=foreign.group(2) if foreign else None,
                    points=row.points,
                )
            )

        # Early payoff ("Erken Kapama ... İptal") closes the cash advance plan billed on the same day,
        # or the last cash advance plan of the statement.
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
