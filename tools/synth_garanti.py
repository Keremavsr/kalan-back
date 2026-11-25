"""Generate synthetic Garanti BBVA Bonus statements for tests.

Writes 12 PDFs plus one ground-truth JSON per PDF into ``tests/fixtures/garanti/``. The PDFs follow
the line structure ``kalan.pdf.extract_text()`` yields for real statements: header label, then value on
the next line; the table ``İşlem Tarihi | Dönem İçi İşlemler | Kalan Borç / Taksit | Bonus (TL) |
Tutar (TL)`` with rows grouped under section and sector titles, ``bosluk`` filler lines, the bonus cell
before the amount, credits with a trailing sign; a page header repeated on every continuation page; a
``Toplam`` row; the ``EKSTRE ÖZETİ`` sentence with the cut-off dates and the label-first summary.
As in the real text layer, a row's cells share one baseline, the description cell is padded with real
space characters that run past the amount column, a sector title starts with a run of spaces and one
filler line holds spaces only.
Every merchant, amount, name, address and number is invented; the card ends in ``1111`` and the holder
is ``ORNEK KISI``.

The ground truth comes from this script's own simulation of the account, not from the parser.

Usage: ``python tools/synth_garanti.py [output_dir]``. Deterministic output. Needs ``pymupdf-fonts``.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pymupdf

from kalan.amounts import add_months
from synth_common import (
    FONT_SIZE,
    LINE,
    WEEKDAYS_TR,
    Layout,
    Plan,
    installment_truth,
    merchant_of,
    money,
    month_date,
    open_plans,
    render,
    shift_month,
    tr_date,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "tests" / "fixtures" / "garanti"
SEED = 20261127
COUNT = 12
FIRST_MONTH = "2026-01"
CUT_DAYS = [26, 25, 25, 26, 25, 26, 27, 26, 25, 27, 26, 25, 26]  # the 13th is the next cut after the last
CARD = "5400 61** **** 1111"
VIRTUAL_CARD = "5400 61** **** 2222"
HOLDER = "ORNEK KISI"
LIMIT = 12_000_000
CASH_LIMIT = 2_000_000
RATE_LINES = (
    "Aylık güncel faiz oranları: Akdi faiz: %3,11 , Gecikme faizi: %3,41, Nakit avans faizi: %4,02, "
    "Limit aşım faizi: %3,11 , Nakit Avans Gecikme faizi: %4,32",
    "Yıllık güncel faiz oranları: Akdi faiz: %37,32, Gecikme faizi: %40,92, Nakit avans faizi: %48,24, "
    "Limit aşım faizi: %37,32 , Nakit Avans Gecikme faizi: %51,84",
)
EXPECTED_RATES = {
    "purchase_monthly": "3.11", "purchase_late_monthly": "3.41", "cash_advance_monthly": "4.02",
    "over_limit_monthly": "3.11", "cash_advance_late_monthly": "4.32", "purchase_yearly": "37.32",
    "purchase_late_yearly": "40.92", "cash_advance_yearly": "48.24", "over_limit_yearly": "37.32",
    "cash_advance_late_yearly": "51.84",
}
PARTNERS = ["ORNEKMARKET-0001-MERKEZ", "DENEME KASAP", "ORNEK YEMEK SIPARIS", "ORNEKPAY    *ORNEKCARSI"]
SECTORS = {
    "Süpermarket": ["DENEME PAZARI", "ORNEK MANAV"],
    "Cafe & Restaurant": ["ORNEK KAHVE EVI", "DENEME LOKANTASI"],
    "Ulaşım": ["ORNEK TAKSI POS"],
}
PLANS = [
    Plan("P1", "ORNEK ELEKTRONIK AVM", 900_000, 6, start=3, day=4),
    Plan("P2", "DENEME MOBILYA", 300_001, 3, start=8, day=6),
    Plan("P4", "DENEME MOBILYA", 150_000, 3, start=8, day=6),  # same shop, same day, other total
    Plan("P3", "ORNEK SPOR MAGAZASI", 480_000, 4, start=10, day=3),
]
BONUS_PLANS = {"P1", "P3"}
SCENARIOS = {
    0: ["first statement: no opening balance row, no cut-off sentence"],
    2: ["zero balance"],
    3: ["installment start", "foreign currency"],
    4: ["partial payment"],
    5: ["interest with BSMV / KKDF", "long table"],
    6: ["bonus spent row"],
    7: ["foreign currency", "foreign merchant in TL"],
    8: ["last installment", "two plans same shop and day", "installment start", "refund", "annual fee"],
    9: ["virtual card section"],
    10: ["last installment", "installment start", "foreign refund"],
    11: ["long table", "plan P3 continues beyond the last statement"],
}


def cut_date(i: int) -> date:
    if i < 0:  # before the first statement: same day one month earlier
        return shift_month(cut_date(0), i)
    return month_date(add_months(FIRST_MONTH, i), CUT_DAYS[i])


@dataclass
class Row:
    date: date | None
    description: str
    amount: int
    credit: bool = False
    kind: str = "purchase"
    cell: str | None = None  # installment or foreign cell
    bonus: str | None = None
    original: tuple[int, str] | None = None
    installment: dict | None = None
    section: str = ""
    sector: str = ""
    bonus_only: bool = False

    def signed(self) -> int:
        return -self.amount if self.credit else self.amount

    def amount_cell(self) -> str:
        return money(self.amount) + ("+" if self.credit else "")

    def truth(self, carry_date: date) -> dict:
        return {
            "date": (self.date or carry_date).isoformat(),
            "description": self.description,
            "amount": 0 if self.bonus_only else self.amount,
            "currency": "TRY",
            "direction": "credit" if self.credit or self.bonus_only else "debit",
            "kind": self.kind,
            "merchant": merchant_of(self.description, self.kind),
            "source": "statement",
            "original_amount": self.original[0] if self.original else None,
            "original_currency": self.original[1] if self.original else None,
            "points": self.bonus,
            "detail": None,
            "installment": self.installment,
        }


TOP_SECTION = ""
EXTRA = "BONUS PLATINUM SAHİBİ OLDUĞUNUZ İÇİN EKSTRA BONUS KAZANDIĞINIZ İŞLEMLER"
PARTNER = "BONUS PROGRAM ORTAKLARI'NDA YAPTIĞINIZ HARCAMALAR"
OTHERS = "BONUS PROGRAM ORTAKLARI DIŞI HARCAMALARINIZ - Bonus işyerlerini tercih edin, daha çok bonus kazanın!"
MISC = "DİĞER HARCAMALARINIZ"
ABROAD = "YURT DIŞI HARCAMALARINIZ"
VIRTUAL = f"BONUS SANAL KART BİLGİLERİNİZ: {HOLDER} / {VIRTUAL_CARD}"
SECTION_ORDER = [TOP_SECTION, PARTNER, OTHERS, MISC, ABROAD, VIRTUAL]


@dataclass
class Sim:
    index: int
    previous: int
    rows: list[Row]

    @property
    def period_end(self) -> date:
        return cut_date(self.index)

    @property
    def due(self) -> date:
        return self.period_end + timedelta(days=10)

    def bucket(self, kinds: set[str], exclude: bool = False) -> int:
        return sum(r.signed() for r in self.rows
                   if (r.kind in kinds) != exclude and r.kind != "carry_over" and not r.bonus_only)

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
        return (self.total * 40 + 5_000) // 10_000 * 100

    @property
    def bonus_earned(self) -> int:
        return sum(int(r.bonus.replace(".", "").replace(",", "")) for r in self.rows if r.bonus and not r.bonus_only)

    @property
    def bonus_spent(self) -> int:
        return sum(r.amount for r in self.rows if r.bonus_only)


def bonus_of(amount: int) -> str | None:
    b = amount // 200
    return money(b) if b else None


def simulate() -> list[Sim]:
    rng = random.Random(SEED)
    sims: list[Sim] = []
    previous = 0
    for i in range(COUNT):
        end = cut_date(i)
        start = cut_date(i - 1) + timedelta(days=1)

        def day(n: int, start: date = start, end: date = end) -> date:
            return min(start + timedelta(days=n), end)

        rows: list[Row] = []
        if i > 0:
            rows.append(Row(None, "ÖNCEKİ DÖNEMDEN DEVİR EDİLEN TUTAR", previous, kind="carry_over"))
        if previous:
            paid = previous // 2 if i == 4 else previous
            rows.append(Row(cut_date(i - 1) + timedelta(days=10), "ÖDEMENİZ İÇİN TEŞEKKÜR EDERİZ", paid,
                            credit=True, kind="payment"))
        if i == 6:
            rows.append(Row(day(12), "BONUS BEDAVA ALIŞVERİŞ", 1_578, credit=True, kind="other", bonus="15,78-",
                            bonus_only=True))
        if i != 2:
            n = 22 if i in (5, 11) else 2 + i % 3
            for s in range(n):
                amount = rng.randrange(4_000, 260_000)
                rows.append(Row(day(1 + (s * 2) % 26), PARTNERS[(i + s) % len(PARTNERS)], amount,
                                bonus=bonus_of(amount), section=PARTNER))
            for sector, shops in SECTORS.items():
                for s, shop in enumerate(shops[: 1 + (i + len(sector)) % 2]):
                    rows.append(Row(day(3 + s * 5), shop, rng.randrange(5_000, 90_000), section=OTHERS,
                                    sector=sector))
                if i in (5, 11):
                    rows.extend(Row(day(6 + s), shops[0], rng.randrange(5_000, 90_000), section=OTHERS,
                                    sector=sector) for s in range(6))
            rows.append(Row(day(9), "ORNEK ULASIM KART", rng.randrange(2_000, 30_000), section=MISC))
        if i == 5:
            for desc, amount, kind in (("AKDİ FAİZ", 52_140, "interest"), ("BSMV", 7_821, "fee"),
                                       ("KKDF", 7_821, "fee")):
                rows.append(Row(end, desc, amount, kind=kind, section=MISC))
        if i == 8:
            rows.append(Row(end, "YILLIK KART ÜCRETİ", 143_950, kind="fee", section=MISC))
            rows.append(Row(day(7), "İADE ORNEK GIYIM", 45_000, credit=True, kind="refund", section=OTHERS,
                            sector="Giyim"))
        if i in (3, 7):
            rows.append(Row(day(14), "ORNEKGAMES.COM 1234567", 42_312, cell="12,34 USD", original=(1_234, "USD"),
                            section=ABROAD))
        if i == 7:
            rows.append(Row(day(16), "Ornek Video Premium", 7_999, cell="79,99 TL", section=ABROAD))
        if i == 10:
            rows.append(Row(day(4), "Ornek Bulut Depolama", 17_126, credit=True, kind="refund", cell="4,99 USD",
                            original=(499, "USD"), section=ABROAD))
        if i == 9:
            rows.append(Row(day(18), "ORNEK DIJITAL ABONELIK", 14_999, section=VIRTUAL))
            rows.append(Row(day(19), "DENEME UYGULAMA MAGAZASI", 3_499, section=VIRTUAL))
        for p in PLANS:
            k = p.k_at(i)
            if k is None:
                continue
            amount = p.amount(k)
            rows.append(Row(end - timedelta(days=p.day), p.description, amount, kind="installment",
                            cell=f"{money(p.each)}x{p.n}={money(p.total)} {k}.Taksit",
                            bonus=bonus_of(amount) if p.key in BONUS_PLANS else None,
                            installment=installment_truth(p, k),
                            section=PARTNER if p.key in BONUS_PLANS else OTHERS,
                            sector="" if p.key in BONUS_PLANS else "Mobilya"))
        order = {s: n for n, s in enumerate(SECTION_ORDER)}
        rows.sort(key=lambda r: (order[r.section], r.sector))  # stable: keeps printing order inside a group
        sim = Sim(i, previous, rows)
        sims.append(sim)
        previous = sim.total
    return sims


# ------------------------------------------------------------------------------------------ layout

TABLE_HEADER = ["İşlem Tarihi", "Dönem İçi İşlemler", "Kalan Borç / Taksit", "Bonus (TL)", "Tutar (TL)"]
BANK_LINES = ["T. Garanti Bankası A.Ş. Genel Müdürlük : Örnek Mah. Deneme Cad. No:1 İstanbul www.garantibbva.com.tr",
              "Büyük Mükellefler V.D. 0000000000"]


class GLayout(Layout):
    def __init__(self, sim: Sim) -> None:
        super().__init__()
        self.sim = sim
        self.table_breaks = 0

    def header(self) -> None:
        for x, text in zip((40, 100, 250, 380, 470), TABLE_HEADER, strict=True):
            self.put(x, text)
        self.y += LINE * 1.3

    def continuation(self) -> None:
        self.new_page()
        self.table_breaks += 1
        for text in BANK_LINES:
            self.line((40, text))
        s = self.sim
        harcanabilir = money(s.bonus_earned - s.bonus_spent + 1_000)
        for label, value in (("Kart Numarası", CARD), ("Son Ödeme Tarihi", tr_date(s.due, pad=True)),
                             ("Harcanabilir Bonus (TL)", harcanabilir), ("Toplam Borcunuz", f"{money(s.total)} TL")):
            self.line((40, label), (200, value))
        self.header()
        self.line((40, "Ekstre No : 1234567890"))
        self.line((40, "Ekstre Sayfası PAGE / PAGES"))
        self.line((40, "bosluk"))

    def ensure(self, n: int) -> None:
        if not self.fits(n):
            self.continuation()


PAD = 100  # spaces the real text layer prints around some cells
PAD_REACH = 490  # a padded description cell ends here, past the start of the amount column (470)
_MEASURE = pymupdf.Font("notos")


def padded(x: float, text: str) -> str:
    """``text`` followed by real space characters up to ``PAD_REACH``, as the real text layer pads its cells.

    The padding runs past the next cells, so they start behind the pen: a reader must still split them into
    their own lines and must not let the spaces bridge the gap.
    """
    room = PAD_REACH - x - _MEASURE.text_length(text, FONT_SIZE)
    return text + " " * max(1, math.ceil(room / _MEASURE.text_length(" ", FONT_SIZE)))


def layout(sim: Sim) -> GLayout:
    lay = GLayout(sim)
    for text in BANK_LINES:
        lay.line((40, text))
    lay.line((40, "Şeffaf Bonus Platinum Bilgileriniz"))
    for label, value in (("Müşteri Numarası", "12345678"), ("Müşteri Limiti", f"{money(LIMIT)} TL"),
                         ("Kart Numarası", CARD), ("Kart Limiti", f"{money(LIMIT)} TL"),
                         ("Nakit Avans Limiti", f"{money(CASH_LIMIT)} TL"),
                         ("Hesap Kesim Tarihi", tr_date(sim.period_end, pad=True)),
                         ("Son Ödeme Tarihi", tr_date(sim.due, pad=True)),
                         ("Dönem Borcunuz", f"{money(sim.total)} TL"),
                         ("Min. Ödeme Tutarı", f"{money(sim.minimum)} TL")):
        lay.line((40, label), (200, value))
    lay.line((40, "Bonusunuzu Hemen Harcayın"))
    lay.line((40, "Bonus"))
    for label, value in (("Bugüne Kadar Kazanılan Toplam Bonus", 10_000 + sim.bonus_earned),
                         ("Bu Dönem Kazanılan Bonus", sim.bonus_earned),
                         ("Bu Dönem Harcanan Bonus", -sim.bonus_spent),
                         ("Harcanabilecek Toplam Bonus", 1_000 + sim.bonus_earned - sim.bonus_spent)):
        lay.line((40, label), (200, f"{money(value)} TL"))
    lay.line((300, f"Son Ödeme Tarihi: {tr_date(sim.due, pad=True)}"))
    lay.line((300, f"Sayın {HOLDER}"))
    lay.line((300, "ORNEK MAH. DENEME SOK. NO: 1 D: 2"))
    lay.line((300, "34000 ORNEKKOY / ISTANBUL"))
    lay.line((300, "Ekstre No : 1234567890"))
    lay.line((300, "Ekstre Sayfası PAGE / PAGES"))
    lay.line((40, "Dikkat! Bu hesap özeti test amaçlı sentetik bir belgedir."))
    lay.header()
    lay.line((40, "bosluk"))
    lay.line((40, " " * PAD))
    lay.line((40, "bosluk"))

    section, sector = None, None
    for r in sim.rows:
        if r.section != section:
            section, sector = r.section, None
            if r.section == PARTNER:
                lay.ensure(6)
                lay.line((40, "bosluk"))
                lay.line((40, EXTRA))
                lay.line((40, "bosluk"))
                lay.line((40, "bosluk"))
                lay.line((40, PARTNER))
                lay.line((40, "bosluk"))
            elif r.section:
                lay.ensure(4)
                lay.line((40, "bosluk"))
                lay.line((40, r.section))
                lay.line((40, "bosluk"))
        if r.sector and r.sector != sector:
            sector = r.sector
            lay.ensure(2)
            lay.line((40, " " * PAD + r.sector))
        lay.ensure(2)
        cells = [(40, tr_date(r.date, pad=True))] if r.date is not None else []
        cells.append((112, padded(112, r.description)))
        if r.cell:
            cells.append((250, r.cell))
        if r.bonus:
            cells.append((380, r.bonus))
        if not r.bonus_only:
            cells.append((470, r.amount_cell()))
        lay.line(*cells)
    lay.ensure(6)
    lay.line((40, "Bonus Card'la ATMlerimizden nakit avans kullanabilirsiniz."))
    lay.line((40, "bosluk"))
    lay.line((40, "Toplam"), (380, money(sim.bonus_earned - sim.bonus_spent)), (470, money(sim.total)))
    lay.line((40, "bosluk"))

    if not lay.fits(24):
        lay.new_page()
        for text in BANK_LINES:
            lay.line((40, text))
    lay.line((40, "EKSTRE ÖZETİ"))
    if sim.index > 0:
        nxt, prev = cut_date(sim.index + 1), cut_date(sim.index - 1)
        nxt_due = nxt + timedelta(days=10)
        prev_words = tr_date(prev, pad=True).rsplit(" ", 1)
        lay.line((40, f"Bir sonraki hesap kesiminiz {tr_date(nxt, pad=True)} {WEEKDAYS_TR[nxt.weekday()]} ve son "
                      f"ödemeniz {tr_date(nxt_due, pad=True)} {WEEKDAYS_TR[nxt_due.weekday()]} 'dir. Bir önceki "
                      f"hesap kesim tarihiniz {prev_words[0]}"))
        lay.line((40, f"{prev_words[1]} {WEEKDAYS_TR[prev.weekday()]} 'dir."))
    lay.line((40, f"Hesap kesim tarihinizde kullanılabilir limitiniz {money(LIMIT - sim.total)} TL ,kullanılabilir "
                  f"nakit limitiniz {money(CASH_LIMIT)} TL'dir."))
    lay.line((40, "Otomatik ödeme talimatı verin, ödemelerinizi sizin yerinize biz takip edelim."))
    lay.line((40, "bosluk"))
    for label in ("Önceki Bakiye", "Dönem Harcamaları", "Faiz ve Ücretler", "Ödemeleriniz", "Dönem Borcunuz",
                  "Min. Ödeme Tutarınız"):
        lay.line((40, label))
    for value in (f"{money(sim.previous)} TL +", f"{money(sim.period)} TL +", f"{money(sim.fees)} TL", "-",
                  f"{money(sim.payments)} TL =", f"{money(sim.total)} TL", f"{money(sim.minimum)} TL"):
        lay.line((40, value))
    lay.line((40, "bosluk"))
    for text in RATE_LINES:
        lay.line((40, text))
    lay.line((40, "bosluk"))
    lay.line((40, "Bu belge test amaçlı sentetik bir hesap özetidir ve gerçek bir kişiye ait değildir."))

    lay.new_page()
    for text in BANK_LINES:
        lay.line((40, text))
    lay.line((40, "Ekstre No : 1234567890"))
    lay.line((40, "Ekstre Sayfası PAGE / PAGES"))
    lay.line((40, "EKSTRENİZ İLE İLGİLİ AÇIKLAMALAR"))
    lay.line((40, "Ekstreniz ile ilgili bilgiyi Garanti BBVA Mobil ve Garanti BBVA Internet'ten alabilirsiniz."))
    n = len(lay.pages)
    for p_no, page in enumerate(lay.pages, start=1):
        for j, (x, y, text) in enumerate(page):
            if "PAGES" in text:
                page[j] = (x, y, text.replace("PAGES", str(n)).replace("PAGE", str(p_no)))
    return lay


def truth(sim: Sim, page_count: int, table_breaks: int) -> dict:
    carry_date = cut_date(sim.index - 1)
    nxt = cut_date(sim.index + 1)
    tags = SCENARIOS.get(sim.index, ["regular month"]) + (["table over a page break"] if table_breaks else [])
    return {
        "synthetic": True,
        "scenarios": tags,
        "statement": {
            "bank": "garanti",
            "product": "Bonus",
            "parser_id": "tr_garanti_bonus",
            "card_last4": "1111",
            "period_start": (carry_date + timedelta(days=1)).isoformat(),
            "period_end": sim.period_end.isoformat(),
            "due_date": sim.due.isoformat(),
            "currency": "TRY",
            "statement_total": sim.total,
            "minimum_payment": sim.minimum,
            "credit_limit": LIMIT,
            "next_period_end": nxt.isoformat() if sim.index > 0 else None,
            "next_due_date": (nxt + timedelta(days=10)).isoformat() if sim.index > 0 else None,
            "page_count": page_count,
            "interest_rates": EXPECTED_RATES,
            "printed_summary": {
                "previous_balance": sim.previous,
                "period_transactions": sim.period,
                "interest_and_fees": sim.fees,
                "payments": sim.payments,
                "statement_total": sim.total,
                "remaining_installments": None,
            },
        },
        "transactions": [r.truth(carry_date) for r in sim.rows],
        "expected_reconciliation": {
            "status": "ok",
            "expected": sim.period,
            "computed": sim.period,
            "difference": 0,
            "checks": ["period_transactions", "interest_and_fees", "payments", "previous_balance",
                       "printed_summary_arithmetic", "statement_total"],
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
        name = f"gb-{sim.index + 1:02d}-{sim.period_end.isoformat()}"
        lay = layout(sim)
        pdf = out_dir / f"{name}.pdf"
        render(lay, pdf, font, "kalan synth_garanti")
        write_json(out_dir / f"{name}.json", truth(sim, len(lay.pages), lay.table_breaks))
        written.append(pdf)
    return written


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT_DIR
    for path in main(target):
        print(path.name)
