"""Generate synthetic Yapı Kredi World statements for tests.

Writes 12 PDFs plus one ground-truth JSON per PDF into ``tests/fixtures/yapikredi/``. The PDFs follow
the line structure PyMuPDF ``page.get_text()`` yields for real statements: header label, then
``: value`` on the next line; one table per card (plastic, digital) with
``İşlem Tarihi | İşlemler | Tutar(TL) | Kalan Tutar/Taksit | Puan``; an undated opening balance row;
sub-lines under rows (``ABNO ... FTNO ...``, ``<total> TL'lik işlemin k / n taksidi``,
``İşlem Tutarı: ...``); a ``TOPLAM`` row per table. Every merchant, amount, name, address and number is
invented; the card ends in ``4321`` and the holder is ``ORNEK KISI``.

The ground truth comes from this script's own simulation of the account, not from the parser.

Usage: ``python tools/synth_yapikredi.py [output_dir]``. Deterministic output. Needs ``pymupdf-fonts``.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf

from kalan.amounts import add_months
from synth_common import (
    LINE,
    Layout,
    Plan,
    installment_truth,
    merchant_of,
    money,
    month_date,
    open_plans,
    render,
    tr_date,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "fixtures" / "yapikredi"
SEED = 20261126
COUNT = 12
FIRST_MONTH = "2025-12"
CUT_DAY = 28
CARD = "5400 61** **** 4321"
DIGITAL_CARD = "5400 61** **** 8765"
HOLDER = "ORNEK KISI"
LIMIT = 15_000_000
CASH_LIMIT = 1_500_000
RATES = [("3,11", "37,32"), ("4,02", "48,24"), ("3,41", "40,92"), ("4,32", "51,84")]
EXPECTED_RATES = {
    "purchase_monthly": "3.11", "purchase_yearly": "37.32", "cash_advance_monthly": "4.02",
    "cash_advance_yearly": "48.24", "purchase_late_monthly": "3.41", "purchase_late_yearly": "40.92",
    "cash_advance_late_monthly": "4.32", "cash_advance_late_yearly": "51.84",
}
BILLS = ["ORNEK ELEKTRIK DAGITIM", "ORNEK SU IDARESI", "ORNEK TELEKOM", "ORNEKNET INTERNET"]
SHOPS = ["ORNEKMARKET          ISTANBUL       TR", "DENEME KAFE          ISTANBUL       TR",
         "ORNEK KITAPEVI       ANKARA         TR", "TEST ECZANESI        IZMIR          TR",
         "ORNEKPAY    *ORNEK YEMEK ISTANBUL     TR"]
PLANS = [
    Plan("A", "ORNEKPAY    *ORNEK ELEKTRONIK ISTANBUL TR", 3_000_000, 6, start=1, day=3),
    Plan("B", "DENEME MOBILYA       ISTANBUL       TR", 900_001, 3, start=6, day=9),
    Plan("D", "DENEME MOBILYA       ISTANBUL       TR", 450_000, 3, start=6, day=9),  # same shop, same day
    Plan("C", "ORNEK SPOR           ISTANBUL       TR", 1_200_000, 4, start=9, day=12),
]
SCENARIOS = {
    0: ["zero balance", "opening balance only, no TOPLAM row"],
    1: ["installment start"],
    3: ["digital card table", "foreign currency"],
    4: ["partial payment"],
    5: ["annual fee", "interest"],
    6: ["last installment", "two plans same shop and day", "installment start"],
    7: ["three-page table", "digital card table", "card-not-present TRY sub-line"],
    8: ["refund", "last installment"],
    9: ["installment start", "foreign currency"],
    11: ["plan C continues beyond the last statement"],
}


def cut_date(i: int) -> date:
    return month_date(add_months(FIRST_MONTH, i), CUT_DAY)


@dataclass
class Row:
    date: date | None  # None: opening balance
    description: str
    amount: int
    credit: bool = False
    kind: str = "purchase"
    cell: str | None = None  # Kalan Tutar/Taksit
    points: str | None = None
    sub: list[str] = field(default_factory=list)  # printed sub-lines
    detail: str | None = None  # expected detail after redaction
    original: tuple[int, str] | None = None
    installment: dict | None = None

    def signed(self) -> int:
        return -self.amount if self.credit else self.amount

    def amount_text(self) -> str:
        return ("+" if self.credit else "") + money(self.amount)

    def truth(self, carry_date: date) -> dict:
        return {
            "date": (self.date or carry_date).isoformat(),
            "description": self.description,
            "amount": self.amount,
            "currency": "TRY",
            "direction": "credit" if self.credit else "debit",
            "kind": self.kind,
            "merchant": merchant_of(self.description, self.kind),
            "source": "statement",
            "original_amount": self.original[0] if self.original else None,
            "original_currency": self.original[1] if self.original else None,
            "points": self.points,
            "detail": self.detail,
            "installment": self.installment,
        }


@dataclass
class Sim:
    index: int
    period_end: date
    previous: int
    card_rows: list[Row]
    digital_rows: list[Row]

    @property
    def rows(self) -> list[Row]:
        return self.card_rows + self.digital_rows

    def bucket(self, kinds: set[str], exclude: bool = False) -> int:
        return sum(r.signed() for r in self.rows if (r.kind in kinds) != exclude and r.kind != "carry_over")

    @property
    def payments(self) -> int:
        return -self.bucket({"payment"})

    @property
    def fees(self) -> int:
        return self.bucket({"fee", "interest"})

    @property
    def period(self) -> int:
        return self.bucket({"payment", "fee", "interest"}, exclude=True)

    @property
    def total(self) -> int:
        return self.previous + self.period + self.fees - self.payments

    @property
    def minimum(self) -> int:
        return (self.total * 40 + 50) // 100

    @property
    def remaining_installments(self) -> int:
        return sum(p.remaining_after(k)[1] for p in PLANS if (k := p.k_at(self.index)) is not None)


def bill_row(rng: random.Random, d: date, name: str) -> Row:
    ab, ft = rng.randrange(1_000_000, 9_999_999), rng.randrange(10_000_000, 99_999_999)
    pad = " " * rng.choice((16, 18, 20))
    return Row(d, name, rng.randrange(8_000, 180_000), sub=[f"ABNO {ab}{pad}FTNO {ft}"],
               detail="ABNO [NUMBER] FTNO [NUMBER]")


def simulate() -> list[Sim]:
    rng = random.Random(SEED)
    sims: list[Sim] = []
    previous = 0
    for i in range(COUNT):
        end = cut_date(i)
        start = cut_date(i - 1) + timedelta(days=1)
        rows: list[Row] = [Row(None, "ÖNCEKİ DÖNEM HESAP ÖZETİ BORCU", previous, kind="carry_over")]
        digital: list[Row] = []
        if i == 0:  # first statement of a new card: nothing happened
            sims.append(Sim(i, end, previous, rows, digital))
            continue
        if previous:
            paid = previous // 2 if i == 4 else previous
            rows.append(Row(start + timedelta(days=9), "ÖDEME-İNTERNET BANKACILIĞI", paid, credit=True, kind="payment"))

        def day(n: int, start: date = start) -> date:
            return start + timedelta(days=n)

        dated: list[Row] = []
        for b, name in enumerate(BILLS[: 2 + i % 3]):
            dated.append(bill_row(rng, day(12 + b * 3), name))
        n_shop = 40 if i == 7 else 1 + i % 3
        for s in range(n_shop):
            shop = SHOPS[(i + s) % len(SHOPS)]
            r = Row(day(2 + (s * 3) % 26), shop, rng.randrange(2_500, 250_000))
            if s == 0 and i in (2, 5, 9):
                r.points = str(rng.randrange(1, 900))
            dated.append(r)
        if i == 3:
            dated.append(Row(day(15), "ORNEK YAZILIM ABONELIK LONDON       GBGB", 43_125,
                             sub=["İşlem Tutarı: 12,50 USD TRY Karşılığı : 431,25 TRY"],
                             detail="İşlem Tutarı: 12,50 USD TRY Karşılığı : 431,25 TRY", original=(1_250, "USD")))
        if i == 9:
            dated.append(Row(day(20), "DENEME OTEL          ROMA           IT", 1_027_440,
                             sub=["İşlem Tutarı: 280,00 EUR TRY Karşılığı : 10.274,40 TRY"],
                             detail="İşlem Tutarı: 280,00 EUR TRY Karşılığı : 10.274,40 TRY", original=(28_000, "EUR")))
        if i == 5:
            dated.append(Row(end, "YILLIK KART UCRETI", 125_000, kind="fee"))
            dated.append(Row(end, "DÖNEM FAİZİ", 31_240, kind="interest"))
        if i == 8:
            dated.append(Row(day(6), "IADE ORNEK GIYIM     ISTANBUL       TR", 45_000, credit=True, kind="refund"))
        dated.sort(key=lambda r: r.date)
        rows.extend(dated)
        for p in PLANS:
            k = p.k_at(i)
            if k is None:
                continue
            bought = cut_date(p.start - 1) + timedelta(days=p.day)  # inside the first billing period
            count, rest = p.remaining_after(k)
            line = f"{money(p.total)} TL'lik işlemin {k} / {p.n} taksidi"
            rows.append(Row(bought, p.description, p.amount(k), kind="installment",
                            cell=f"{money(rest)} / {count}" if count else None,
                            points=f"{p.amount(k) // 500:,}".replace(",", ".") if count else None,
                            sub=[line], detail=line, installment=installment_truth(p, k)))
        if i in (3, 7):
            digital.append(Row(day(8), "ORNEK DIJITAL OYUN   ISTANBUL       TR", 99_999,
                               sub=["İşlem Tutarı: 999,99 TRY TRY Karşılığı : 999,99 TRY"] if i == 7 else [],
                               detail="İşlem Tutarı: 999,99 TRY TRY Karşılığı : 999,99 TRY" if i == 7 else None))
        sim = Sim(i, end, previous, rows, digital)
        sims.append(sim)
        previous = sim.total
    return sims


# ------------------------------------------------------------------------------------------ layout

TABLE_HEADER = [(40, "İşlem Tarihi"), (110, "İşlemler"), (360, "Tutar(TL)"), (430, "Kalan Tutar/Taksit"),
                (530, "Puan")]


def footer(sim: Sim):
    def _f(lay: Layout) -> None:
        n = len(lay.pages)
        texts = ["Temassız İşlem", "YAPI ve KREDİ BANKASI A.Ş.", "Büyük Mükellefler V.D. / 0000000000",
                 f"Sıra No: 0000000001 Seri No: 1 Düzenlenme Tarihi: {sim.period_end.strftime('%d.%m.%Y')}",
                 f"{n} / PAGES     {100 + n}"]
        for j, text in enumerate(texts):
            lay.put(40, text, y=790 + j * 8)
    return _f


def table(lay: Layout, card_lines: list[tuple[float, str]], rows: list[Row], sim: Sim, total: int) -> None:
    def head() -> None:
        for cell in card_lines:
            lay.line(cell)
        lay.line(*TABLE_HEADER, gap=LINE * 1.3)

    head()
    for r in rows:
        need = 3 + len(r.sub) + (1 if r.cell else 0)
        if not lay.fits(need + 3):
            lay.new_page()
            lay.table_breaks += 1
            head()
        if r.date is not None:
            lay.line((40, tr_date(r.date, pad=True)))
        lay.line((110, r.description))
        lay.line((360, r.amount_text()))
        if r.cell:
            lay.line((430, r.cell))
        if r.points:
            lay.line((530, r.points))
        for s in r.sub:
            lay.line((110, s))
    if len(rows) > 1 or rows[0].kind != "carry_over":
        lay.line((40, "TOPLAM"), (360, money(total)), (530, "0"))


def layout(sim: Sim) -> Layout:
    lay = Layout()
    lay.table_breaks = 0
    lay.footer = footer(sim)
    due = sim.period_end + timedelta(days=10)
    next_end = cut_date(sim.index + 1)
    lay.line((300, f"Sayın {HOLDER}"))
    lay.line((300, "ORNEK MAH. DENEME SOK. NO: 1 D: 2"))
    lay.line((300, "34000 ORNEKKOY / ISTANBUL"))
    lay.line((300, f"Son Ödeme Tarihi: {tr_date(due)}"))
    lay.line((40, "HESAP BİLGİLERİ"))
    payments = f"+{money(sim.payments)} TL" if sim.payments else "0,00 TL"
    header = [
        ("Hesap Kesim Tarihi", tr_date(sim.period_end)),
        ("Son Ödeme Tarihi", tr_date(due)),
        ("Dönem Borcu", f"{money(sim.total)} TL"),
        ("Ödenmesi Gereken Asgari Tutar/Oran", f"{money(sim.minimum)} TL / 40%"),
        ("Otomatik Ödeme Talimatı Oranı", "%40"),
        ("Önceki Dönem Hesap Özeti Borcu", f"{money(sim.previous)} TL"),
        ("Dönem İçi Harcamalar", f"{money(sim.period + sim.fees)} TL"),
        ("Dönem İçi Ödemeler", payments),
        ("Kalan Toplam Taksit Tutarı", f"{money(sim.remaining_installments)} TL"),
        ("Kullanılabilir Toplam Worldpuan/TL Karşılığı", "0 / 0,00 TL"),
        ("Bir Sonraki Ay Hesap Kesim Tarihi", tr_date(next_end)),
        ("Bir Sonraki Ay Son Ödeme Tarihi", tr_date(next_end + timedelta(days=10))),
        ("Müşteri Limiti", f"{money(LIMIT)} TL"),
        ("Kart Limiti", f"{money(LIMIT)} TL"),
        ("Nakit Çekim Limiti", f"{money(CASH_LIMIT)} TL"),
        ("Kart Numarası", CARD),
        ("Müşteri Numarası", "12345678"),
    ]
    for label, value in header:
        lay.line((40, label), (200, f": {value}"))
    card_total = sum(r.signed() for r in sim.card_rows)
    table(lay, [(40, "Kart Numarası"), (40, CARD), (40, HOLDER)], sim.card_rows, sim, card_total)
    if sim.digital_rows:
        if not lay.fits(12):
            lay.new_page()
        lay.line((40, "Dijital Kart Numarası"))
        lay.line((200, f": {DIGITAL_CARD}"))
        table(lay, [(40, HOLDER)], sim.digital_rows, sim, sum(r.signed() for r in sim.digital_rows))
    if not lay.fits(16):
        lay.new_page()
    lay.line((40, "PUAN ÖZETİ"))
    lay.line((40, "TÜM ÜYE İŞYERLERİNDE GEÇERLİ WORLDPUAN BİLGİLERİ"))
    for label in ("Devreden", "Bu Ay Kazanılan", "Bu Ay Kullanılan", "Kalan", "TL Karşılığı"):
        lay.line((40, label))
    lay.line((40, "WORLDPUAN"))
    for value in ("0", "0", "0", "0", "0,00"):
        lay.line((40, value))
    lay.line((40, "Bu belge test amaçlı sentetik bir hesap özetidir ve gerçek bir kişiye ait değildir."))
    lay.close_page()
    rate_texts = ("Alışveriş (Aylık/Yıllık)", "Nakit Çekim (Aylık/Yıllık)", "Akdi Faiz Oranı",
                  f"%{RATES[0][0]} / %{RATES[0][1]}", f"%{RATES[1][0]} / %{RATES[1][1]}", "Gecikme Faiz Oranı",
                  f"%{RATES[2][0]} / %{RATES[2][1]}", f"%{RATES[3][0]} / %{RATES[3][1]}")
    for j, text in enumerate(rate_texts):  # printed on page 1 after its footer, as on the real statement
        lay.pages[0].append((330, 790 + j * 6, text))
    lay.footer = None
    lay.pages.append([])
    lay.y = 40
    lay.line((40, "MESAJINIZ VAR"))
    lay.line((40, "Kredi kartı hesap özetinizi mobil uygulamadan da görüntüleyebilirsiniz."))
    lay.line((40, "Yapı ve Kredi Bankası A.Ş. örnek bilgilendirme metni."))
    lay.line((40, "YAPI KREDİ WORLD KULLANIMIYLA İLGİLİ ALINAN ÜCRETLER NELERDİR?"))
    lay.line((40, "Yıllık üyelik ücreti ve diğer ücretler örnek amaçlıdır."))
    n = len(lay.pages)
    for p in lay.pages:
        for j, (x, y, text) in enumerate(p):
            if "PAGES" in text:
                p[j] = (x, y, text.replace("PAGES", str(n)))
    return lay


def truth(sim: Sim, page_count: int, table_breaks: int) -> dict:
    due = sim.period_end + timedelta(days=10)
    next_end = cut_date(sim.index + 1)
    carry_date = cut_date(sim.index - 1)
    p = sim.period + sim.fees
    return {
        "synthetic": True,
        "scenarios": SCENARIOS.get(sim.index, ["regular month"])
        + (["table over a page break"] if table_breaks else []),
        "statement": {
            "bank": "yapikredi",
            "product": "World",
            "parser_id": "tr_yapikredi_world",
            "card_last4": "4321",
            "period_start": (carry_date + timedelta(days=1)).isoformat(),
            "period_end": sim.period_end.isoformat(),
            "due_date": due.isoformat(),
            "currency": "TRY",
            "statement_total": sim.total,
            "minimum_payment": sim.minimum,
            "credit_limit": LIMIT,
            "next_period_end": next_end.isoformat(),
            "next_due_date": (next_end + timedelta(days=10)).isoformat(),
            "page_count": page_count,
            "interest_rates": EXPECTED_RATES,
            "printed_summary": {
                "previous_balance": sim.previous,
                "period_transactions": p,
                "interest_and_fees": None,
                "payments": sim.payments,
                "statement_total": sim.total,
                "remaining_installments": sim.remaining_installments,
            },
        },
        "transactions": [r.truth(carry_date) for r in sim.rows],
        "expected_reconciliation": {
            "status": "ok",
            "expected": p,
            "computed": p,
            "difference": 0,
            "checks": ["period_transactions", "payments", "previous_balance", "printed_summary_arithmetic",
                       "remaining_installments", "statement_total"],
            "buckets": {"carry_over": sim.previous, "period": sim.period, "interest_and_fees": sim.fees,
                        "payments": sim.payments},
        },
        "open_plans": open_plans(PLANS, sim.index),
    }


def main(out_dir: Path = OUT_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    font = pymupdf.Font("notos")
    written = []
    for sim in simulate():
        name = f"yk-{sim.index + 1:02d}-{sim.period_end.isoformat()}"
        lay = layout(sim)
        pdf = out_dir / f"{name}.pdf"
        render(lay, pdf, font, "kalan synth_yapikredi")
        write_json(out_dir / f"{name}.json", truth(sim, len(lay.pages), lay.table_breaks))
        written.append(pdf)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR
    for path in main(target):
        print(path.name)
