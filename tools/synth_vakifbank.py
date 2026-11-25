"""Generate synthetic VakıfBank Worldcard statements for tests.

Writes 15 PDFs plus one ground-truth JSON per PDF into ``tests/fixtures/vakifbank/``. The PDFs
reproduce the line structure that PyMuPDF ``page.get_text()`` yields for real statements (key, ``:``
and value on separate lines; table rows as date / description / amount / optional cells). Every
merchant, amount, name, address and number is invented; the card is ``4799********0000`` and the
holder ``ORNEK KISI``.

The ground truth is computed by this script's own simulation of the account, not by the parser:
statement fields, every transaction, the expected installment schedule after each statement and the
expected reconciliation buckets.

Usage: ``python tools/synth_vakifbank.py [output_dir]``. Output is deterministic (fixed seed, fixed
PDF metadata). Needs ``pymupdf-fonts`` (dev extra) for a Turkish-capable embedded font.
"""

from __future__ import annotations

import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalan.amounts import add_months, format_minor  # noqa: E402

SEED = 20261125
OUT_DIR = ROOT / "tests" / "fixtures" / "vakifbank"
CARD_PRINTED = "4799********0000"
HOLDER = "ORNEK KISI"
ADDRESS = ["ORNEK MAH. DENEME SOK. NO: 1 D: 2", "34000 ORNEKKOY / ISTANBUL"]
LIMIT = 6_000_000
CA_DESC = "Mobil-Taksitli Nakit Avans"
RATES = [
    ("Alışveriş Faiz Oranı", "3.11", "37.32", "purchase"),
    ("Nakit Çekim Faiz Oranı", "4.02", "48.24", "cash_advance"),
    ("Alışveriş Gecikme Faiz Oranı", "3.41", "40.92", "purchase_late"),
    ("Nakit Gecikme Faiz Oranı", "4.32", "51.84", "cash_advance_late"),
]
# Cut-off dates of the 15 statements (2025-10 .. 2026-12); the one before the first is 2025-09-23.
CUT_DAYS = [24, 23, 22, 23, 24, 23, 22, 24, 23, 22, 24, 23, 22, 24, 23]
FIRST_MONTH = "2025-10"
PREVIOUS_CUT = date(2025, 9, 23)
NEXT_AFTER_LAST = date(2027, 1, 22)


def money(minor: int) -> str:
    return format_minor(minor)


def round_half_up(x: float) -> int:
    return math.floor(x + 0.5)


def cut_dates() -> list[date]:
    out = []
    for i, day in enumerate(CUT_DAYS):
        y, m = (int(p) for p in add_months(FIRST_MONTH, i).split("-"))
        out.append(date(y, m, day))
    return out


# ------------------------------------------------------------------------------------------ rows


@dataclass
class Row:
    date: date
    description: str
    amount: int
    credit: bool = False
    cell: str | None = None
    points: str | None = None
    kind: str = "purchase"
    merchant: str | None = None
    installment: dict | None = None
    original_amount: int | None = None
    original_currency: str | None = None
    order: int = 0

    def amount_text(self) -> str:
        return ("+" if self.credit else "") + money(self.amount)

    def truth(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "description": self.description,
            "amount": self.amount,
            "currency": "TRY",
            "direction": "credit" if self.credit else "debit",
            "kind": self.kind,
            "installment": self.installment,
            "merchant": self.merchant,
            "source": "statement",
            "original_amount": self.original_amount,
            "original_currency": self.original_currency,
            "points": self.points,
        }

    @property
    def signed(self) -> int:
        return -self.amount if self.credit else self.amount


def _cell(remaining: int, each: int) -> str:
    return f"{remaining}x{money(each)}" if remaining > 0 else "Son Taksit"


def _inst(plan: str, remaining: int, each: int, index: int | None, total: int | None, component: str | None) -> dict:
    return {
        "remaining_count": remaining,
        "remaining_amount_each": each if remaining > 0 else 0,
        "is_last": remaining == 0,
        "plan_key": plan,
        "index": index,
        "total_count": total,
        "component": component,
    }


# ----------------------------------------------------------------------------------------- plans


@dataclass
class PurchasePlan:
    key: str
    merchant: str
    price: int
    count: int
    start: int  # statement index of the first installment
    day_offset: int
    marker: str = "slash"  # "slash" -> "2/6", "dot" -> "2.Taksit"

    @property
    def each(self) -> int:
        return self.price // self.count

    def amount(self, i: int) -> int:
        return self.price - self.each * (self.count - 1) if i == 1 else self.each

    def rows(self, k: int, period_start: date) -> list[Row]:
        i = k - self.start + 1
        if not 1 <= i <= self.count:
            return []
        remaining = self.count - i
        marker = f"{i}/{self.count}" if self.marker == "slash" else f"{i}.Taksit"
        return [
            Row(
                date=period_start + timedelta(days=self.day_offset),
                description=f"{self.merchant} {marker}",
                amount=self.amount(i),
                cell=_cell(remaining, self.each),
                kind="installment",
                merchant=self.merchant,
                installment=_inst(self.key, remaining, self.each, i, self.count, None),
            )
        ]

    def state_after(self, k: int) -> tuple[int, int, int] | None:
        """(remaining, amount_each, current index) after statement k, or None if not active."""
        i = k - self.start + 1
        if not 1 <= i < self.count:
            return None
        return self.count - i, self.each, i


@dataclass
class CashAdvancePlan:
    key: str
    principal: int
    count: int
    monthly_rate: float
    start: int
    day_offset: int
    payoff_at: int | None = None  # statement index of early payoff
    principals: list[int] = field(default_factory=list)
    interests: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        r = self.monthly_rate
        annuity = round_half_up(self.principal * r / (1 - (1 + r) ** -self.count))
        balance = self.principal
        for i in range(1, self.count + 1):
            interest = round_half_up(balance * r)
            part = balance if i == self.count else annuity - interest
            self.principals.append(part)
            self.interests.append(interest)
            balance -= part

    @staticmethod
    def tax(interest: int) -> int:
        return round_half_up(interest * 0.15)

    def _avg(self, values: list[int], i: int) -> int:
        rest = values[i:]
        return round_half_up(sum(rest) / len(rest)) if rest else 0

    def components_after(self, i: int) -> dict[str, int]:
        """Remaining-cell amounts printed on installment i, by component."""
        taxes = [self.tax(x) for x in self.interests]
        return {
            "bsmv": self._avg(taxes, i),
            "principal": self._avg(self.principals, i),
            "kkdf": self._avg(taxes, i),
            "interest": self._avg(self.interests, i),
        }

    def rows(self, k: int, period_start: date) -> list[Row]:
        i = k - self.start + 1
        if self.payoff_at is not None and k > self.payoff_at:
            return []
        if not 1 <= i <= self.count:
            return []
        d = period_start + timedelta(days=self.day_offset)
        if self.payoff_at == k:
            return self._payoff_rows(i, d)
        remaining = self.count - i
        after = self.components_after(i)
        interest = self.interests[i - 1]
        tax = self.tax(interest)
        spec = [
            ("BSMV", tax, "bsmv", "fee"),
            ("Taksit Anap", self.principals[i - 1], "principal", "cash_advance"),
            ("KKDF", tax, "kkdf", "fee"),
            ("Vade Farkı", interest, "interest", "interest"),
        ]
        return [
            Row(
                date=d,
                description=f"{CA_DESC} {i}.{label}",
                amount=amount,
                cell=_cell(remaining, after[comp]),
                kind=kind,
                installment=_inst(self.key, remaining, after[comp], i, self.count, comp),
            )
            for label, amount, comp, kind in spec
        ]

    def _payoff_rows(self, first: int, d: date) -> list[Row]:
        rows = []
        for j in range(first, self.count + 1):
            remaining = self.count - j
            each = self._avg(self.principals, j)
            rows.append(
                Row(
                    date=d,
                    description=f"{CA_DESC} {j}.Taksit Anap",
                    amount=self.principals[j - 1],
                    cell=_cell(remaining, each),
                    kind="cash_advance",
                    installment=_inst(self.key, remaining, each, j, self.count, "principal"),
                )
            )
        reversed_interest = round_half_up(self.interests[first - 2] * 0.6) if first > 1 else 0
        payoff = {
            "remaining_count": 0, "remaining_amount_each": 0, "is_last": True, "plan_key": self.key,
            "index": None, "total_count": None, "component": "early_payoff",
        }
        for label, amount in (
            ("Erken Kapama Faiz İptal", reversed_interest),
            ("Erken Kapama BSMV İptal", self.tax(reversed_interest)),
            ("Erken Kapama KKDF İptal", self.tax(reversed_interest)),
        ):
            rows.append(Row(date=d, description=label, amount=amount, credit=True, kind="refund",
                            installment=dict(payoff)))
        return rows

    def state_after(self, k: int) -> tuple[int, dict[str, int], int] | None:
        i = k - self.start + 1
        if self.payoff_at is not None and k >= self.payoff_at:
            return None
        if not 1 <= i < self.count:
            return None
        return self.count - i, self.components_after(i), i


PURCHASE_PLANS = [
    PurchasePlan("A", "ELEKTROMARKET-ORNEK", 1_200_000, 6, start=1, day_offset=6),
    PurchasePlan("B", "TELEFONCU-ORNEK", 450_000, 3, start=2, day_offset=9),
    PurchasePlan("C", "MOBILYA-ORNEK", 2_400_000, 12, start=9, day_offset=4, marker="dot"),
    PurchasePlan("D", "BEYAZESYA-ORNEK", 1_890_000, 3, start=11, day_offset=12),
    PurchasePlan("E", "TATIL-ORNEK", 3_200_000, 4, start=12, day_offset=2),
]
CASH_PLANS = [
    CashAdvancePlan("CA1", 1_000_000, 3, 0.035, start=3, day_offset=17, payoff_at=4),
    CashAdvancePlan("CA2", 800_000, 4, 0.0399, start=7, day_offset=10),
]
EVERYDAY = ["MIGROS-ORNEK", "KAFE-ORNEK", "ECZANE-ORNEK", "AKARYAKIT-ORNEK ISTANBUL", "KITAPCI-ORNEK", "FIRIN-ORNEK"]
SCENARIOS = {
    0: ["zero balance", "one page"],
    1: ["plain purchases", "installment start (A, 6x)", "points cells", "subscriptions"],
    2: ["installment start (B, 3x)", "purchase later refunded"],
    3: ["installment cash advance with BSMV/KKDF/vade farkı (CA1)", "refund"],
    4: ["early payoff of CA1", "last installment (B)", "tax payment"],
    5: ["annual card fee"],
    6: ["last installment (A)", "foreign currency purchase"],
    7: ["installment cash advance (CA2, 4x)"],
    8: ["two-page table", "subscription price rise"],
    9: ["installment start (C, 12x, k.Taksit marker)"],
    10: ["last installment of cash advance (CA2)"],
    11: ["installment start (D, 3x)"],
    12: ["partial payment", "interest with BSMV/KKDF", "installment start (E, 4x)"],
    13: ["last installment (D)"],
    14: ["plans continue beyond the last statement"],
}


# ------------------------------------------------------------------------------------ simulation


@dataclass
class StatementSim:
    index: int
    period_start: date
    period_end: date
    due_date: date
    next_period_end: date
    next_due_date: date
    rows: list[Row]
    previous_balance: int
    period: int
    fees: int
    payments: int
    total: int
    minimum: int
    one_page: bool


def minimum_payment(total: int) -> int:
    if total <= 0:
        return 0
    return math.ceil(total * 0.2 / 100) * 100


def simulate() -> list[StatementSim]:
    rng = random.Random(SEED)
    cuts = cut_dates()
    sims: list[StatementSim] = []
    previous_total = 125_000  # balance of the (not generated) statement before the first
    for k, cut in enumerate(cuts):
        prev_cut = cuts[k - 1] if k else PREVIOUS_CUT
        start = prev_cut + timedelta(days=1)
        span = (cut - start).days

        def day(n: int, _start: date = start, _span: int = span) -> date:
            return _start + timedelta(days=min(n, _span))

        rows: list[Row] = [Row(date=prev_cut, description="ÖNCEKİ DÖNEM HESAP ÖZETİ BAKİYENİZ",
                               amount=previous_total, kind="carry_over", order=-1)]
        if previous_total > 0:
            paid = previous_total - 100_000 if k == 12 else previous_total
            rows.append(Row(date=day(rng.randint(3, 9)), description="ÖDEMENİZ İÇİN TEŞEKKÜRLER", amount=paid,
                            credit=True, kind="payment"))
        else:
            paid = 0

        if k >= 1:
            n_plain = 38 if k == 8 else rng.randint(2, 5)
            for _ in range(n_plain):
                merchant = rng.choice(EVERYDAY)
                amount = rng.randint(2_000, 250_000)
                points = f"{amount * 3 // 1000 / 100:.2f}" if k == 1 else None
                rows.append(Row(date=day(rng.randint(0, span)), description=merchant, amount=amount,
                                points=points, merchant=merchant))
            streamflix = 17_999 if k >= 8 else 14_999
            rows.append(Row(date=day(11), description="STREAMFLIX", amount=streamflix, merchant="STREAMFLIX"))
            rows.append(Row(date=day(14), description="MUZIKBOX-ORNEK", amount=5_999, merchant="MUZIKBOX-ORNEK"))
        if k == 2:
            rows.append(Row(date=day(20), description="GIYIM-ORNEK", amount=89_990, merchant="GIYIM-ORNEK"))
        if k == 3:
            rows.append(Row(date=day(5), description="GIYIM-ORNEK IADE", amount=89_990, credit=True, kind="refund",
                            merchant="GIYIM-ORNEK IADE"))
        if k == 4:
            rows.append(Row(date=day(18), description="Vergi Th/482913/0007", amount=135_000, kind="tax"))
        if k == 5:
            rows.append(Row(date=day(24), description="KART UYELIK AIDATI", amount=87_500, kind="fee"))
        if k == 6:
            rows.append(Row(date=day(13), description="STEAMPLAY-ORNEK BELLEVUE US 19.99 USD", amount=69_000,
                            merchant="STEAMPLAY-ORNEK BELLEVUE US", original_amount=1999, original_currency="USD"))
        if k == 12:
            interest = round_half_up(100_000 * 0.0311)
            tax = round_half_up(interest * 0.15)
            rows.append(Row(date=cut, description="Akdi Faiz", amount=interest, kind="interest"))
            rows.append(Row(date=cut, description="BSMV", amount=tax, kind="fee"))
            rows.append(Row(date=cut, description="KKDF", amount=tax, kind="fee"))
        for plan in PURCHASE_PLANS:
            rows.extend(plan.rows(k, start))
        for ca in CASH_PLANS:
            rows.extend(ca.rows(k, start))

        for n, r in enumerate(rows):
            if r.order == 0:
                r.order = n
        rows.sort(key=lambda r: (r.order >= 0, r.date, r.order))

        period = sum(r.signed for r in rows if r.kind not in ("carry_over", "payment", "fee", "interest"))
        fees = sum(r.signed for r in rows if r.kind in ("fee", "interest"))
        payments = sum(r.amount for r in rows if r.kind == "payment")
        total = previous_total + period + fees - payments
        next_cut = cuts[k + 1] if k + 1 < len(cuts) else NEXT_AFTER_LAST
        sims.append(
            StatementSim(
                index=k, period_start=start, period_end=cut, due_date=cut + timedelta(days=10),
                next_period_end=next_cut, next_due_date=next_cut + timedelta(days=10), rows=rows,
                previous_balance=previous_total, period=period, fees=fees, payments=payments, total=total,
                minimum=minimum_payment(total), one_page=k == 0,
            )
        )
        assert paid == payments
        previous_total = total
    return sims


def expected_schedule(k: int, cut: date, months: int = 12) -> dict:
    month = f"{cut.year:04d}-{cut.month:02d}"
    window = [add_months(month, i) for i in range(1, months + 1)]
    items: dict[str, list[dict]] = {m: [] for m in window}
    plans = []
    for p in PURCHASE_PLANS:
        st = p.state_after(k)
        if st is None:
            continue
        remaining, each, idx = st
        plans.append(("purchase", p.key, p.merchant, remaining, each, idx, p.count, None))
    for ca in CASH_PLANS:
        st = ca.state_after(k)
        if st is None:
            continue
        remaining, comps, idx = st
        plans.append(("cash_advance", ca.key, CA_DESC, remaining, sum(comps.values()), idx, ca.count, comps))
    out_plans = []
    for kind, key, desc, remaining, each, idx, count, comps in plans:
        for i in range(1, remaining + 1):
            m = add_months(month, i)
            if m in items:
                items[m].append({"plan_key": key, "description": desc, "amount": each, "index": idx + i,
                                 "count": count})
        out_plans.append({
            "plan_key": key, "description": desc, "kind": kind, "remaining_count": remaining, "amount_each": each,
            "ends": add_months(month, remaining), "total_remaining": remaining * each, "components": comps,
        })
    return {
        "start_month": window[0],
        "months": [{"month": m, "total": sum(i["amount"] for i in its), "items": its} for m, its in items.items()],
        "plans": sorted(out_plans, key=lambda p: (p["ends"], p["plan_key"])),
    }


# -------------------------------------------------------------------------------------- rendering

PAGE_W, PAGE_H = 595, 842
TOP, BOTTOM = 40, 790
LINE = 11
FONT_SIZE = 7.5


class Layout:
    """Collects positioned text per page; pages are rendered once the page count is known."""

    def __init__(self) -> None:
        self.pages: list[list[tuple[float, float, str]]] = [[]]
        self.y = TOP

    def put(self, x: float, text: str, y: float | None = None) -> None:
        self.pages[-1].append((x, self.y if y is None else y, text))

    def line(self, *cells: tuple[float, str], gap: float = LINE) -> None:
        if self.y + gap > BOTTOM:
            raise OverflowError
        for x, text in cells:
            self.put(x, text)
        self.y += gap

    def new_page(self) -> None:
        self.pages.append([])
        self.y = TOP
        self.line((40, "Kredi Kartı Hesap Özeti (TL)"), gap=LINE * 2)

    def fits(self, n_lines: int) -> bool:
        return self.y + n_lines * LINE <= BOTTOM


TABLE_HEADER = [(40, "İŞLEM TARİHİ"), (100, "AÇIKLAMA"), (380, "TUTAR (TL)"), (450, "KALAN TAKSİT"), (520, "WORLDPUAN")]


def layout_statement(sim: StatementSim) -> Layout:
    lay = Layout()
    lay.line((40, "Kredi Kartı Hesap Özeti (TL)"), gap=LINE * 2)
    lay.line((40, "VakıfBank Worldcard ile harcamalarınızı mobil uygulamadan anında takip edebilirsiniz."))
    lay.line((40, "HESAP BİLGİLERİNİZ"), gap=LINE * 1.5)
    header = [
        ("Dönem Borcunuz", f"{money(sim.total)} TL"),
        ("Asgari Ödeme Tutarı", f"{money(sim.minimum)} TL"),
        ("Son Ödeme Tarihi", sim.due_date.strftime("%d.%m.%Y")),
        ("Şube Adı", "ORNEK SUBESI"),
        ("VB Müşteri No", "1234567890"),
        ("Kart No", CARD_PRINTED),
        ("Limitiniz", f"{money(LIMIT)} TL"),
        ("Hesap Kesim Tarihi", sim.period_end.strftime("%d.%m.%Y")),
        ("Bir Sonraki Hesap Kesim Tarihi", sim.next_period_end.strftime("%d.%m.%Y")),
        ("Bir Sonraki Son Ödeme Tarihi", sim.next_due_date.strftime("%d.%m.%Y")),
    ]
    for key, value in header:
        lay.line((40, key), (170, ":"), (180, value))
    lay.line((40, "Faiz oranlarınızı ekstrenin son bölümünde bulabilirsiniz."), gap=LINE * 1.5)
    lay.line((40, f"00000{sim.index:02d}20261125000001"))
    lay.line((300, f"Son Ödeme Tarihi: {sim.due_date.strftime('%d.%m.%Y')}"))
    lay.line((300, f"Sayın {HOLDER}"))
    for a in ADDRESS:
        lay.line((300, a))
    lay.line((40, "VakıfBank Worldcard kartınızla yurt içi ve yurt dışında güvenle alışveriş yapın."))
    lay.line((40, "Bu ekstre örnek amaçlı üretilmiştir ve gerçek bir kişiye ait değildir."), gap=LINE * 1.5)
    lay.line(*TABLE_HEADER, gap=LINE * 1.3)

    for row in sim.rows:
        if not lay.fits(1):
            lay.new_page()
            lay.line(*TABLE_HEADER, gap=LINE * 1.3)
        cells = [(40, row.date.strftime("%d.%m.%Y")), (100, row.description), (380, row.amount_text())]
        if row.cell:
            cells.append((450, row.cell))
        if row.points:
            cells.append((520, row.points))
        lay.line(*cells)

    summary = [
        ("Önceki Hesap Bakiyesi", money(sim.previous_balance)),
        ("Dönem Içi Islemler", money(sim.period) if sim.period >= 0 else "+" + money(-sim.period)),
        ("Toplam Faiz ve Ücretler", money(sim.fees)),
        ("Ödemeler", "+" + money(sim.payments) if sim.payments else money(0)),
        ("Dönem Borcunuz", money(sim.total)),
    ]
    if not lay.fits(len(summary) + 6):
        lay.new_page()
    lay.y += LINE * 0.5
    lay.line((40, "HESAP ÖZETİ"))
    for label, value in summary:
        lay.line((40, label), (200, value))
    lay.y += LINE * 0.5
    lay.line((40, "WORLDPUAN BİLGİLERİNİZ"))
    lay.line((40, "Kazanılan Puan"), (200, "0"))
    lay.line((40, "Toplam Puan"), (200, "0"))

    if (not sim.one_page and len(lay.pages) == 1) or not lay.fits(len(RATES) * 2 + 4):
        lay.new_page()
    lay.y += LINE * 0.5
    lay.line((40, "FAİZ ORANLARI"))
    lay.line((200, "AYLIK"), (260, "YILLIK"))
    for label, monthly, yearly, _ in RATES:
        lay.line((40, label))
        lay.line((200, f"%{monthly}    %{yearly}"))
    lay.line((40, "Bu belge test amaçlı sentetik bir hesap özetidir."))
    return lay


def render(lay: Layout, path: Path, font: pymupdf.Font) -> None:
    doc = pymupdf.open()
    n = len(lay.pages)
    for i, items in enumerate(lay.pages, start=1):
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_font(fontname="F0", fontbuffer=font.buffer)
        for x, y, text in items:
            page.insert_text((x, y), text, fontname="F0", fontsize=FONT_SIZE)
        page.insert_text((270, 815), f"Sayfa {i} / {n}", fontname="F0", fontsize=FONT_SIZE)
    doc.set_metadata({"title": "Synthetic statement", "producer": "kalan synth_vakifbank",
                      "creationDate": "D:20261125000000", "modDate": "D:20261125000000"})
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def truth(sim: StatementSim, page_count: int) -> dict:
    return {
        "synthetic": True,
        "scenarios": SCENARIOS[sim.index],
        "statement": {
            "bank": "vakifbank",
            "product": "Worldcard",
            "parser_id": "tr_vakifbank_worldcard",
            "card_last4": "0000",
            "period_start": sim.period_start.isoformat(),
            "period_end": sim.period_end.isoformat(),
            "due_date": sim.due_date.isoformat(),
            "currency": "TRY",
            "statement_total": sim.total,
            "minimum_payment": sim.minimum,
            "credit_limit": LIMIT,
            "next_period_end": sim.next_period_end.isoformat(),
            "next_due_date": sim.next_due_date.isoformat(),
            "page_count": page_count,
            "interest_rates": {f"{key}_{part}": val for _, mo, yr, key in RATES
                               for part, val in (("monthly", mo), ("yearly", yr))},
            "printed_summary": {
                "previous_balance": sim.previous_balance,
                "period_transactions": sim.period,
                "interest_and_fees": sim.fees,
                "payments": sim.payments,
                "statement_total": sim.total,
            },
        },
        "transactions": [r.truth() for r in sim.rows],
        "expected_reconciliation": {
            "status": "ok",
            "expected": sim.period,
            "computed": sim.period,
            "difference": 0,
            "buckets": {"carry_over": sim.previous_balance, "period": sim.period, "interest_and_fees": sim.fees,
                        "payments": sim.payments},
        },
        "expected_schedule": expected_schedule(sim.index, sim.period_end),
    }


def main(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    font = pymupdf.Font("notos")
    written = []
    for sim in simulate():
        name = f"vb-{sim.index + 1:02d}-{sim.period_end.isoformat()}"
        lay = layout_statement(sim)
        pdf = out_dir / f"{name}.pdf"
        render(lay, pdf, font)
        data = truth(sim, len(lay.pages))
        (out_dir / f"{name}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        written.append(pdf)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR
    for p in main(target):
        print(p.name)
