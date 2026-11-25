"""Shared pieces of the synthetic statement generators (Yapı Kredi, Garanti BBVA).

Text is placed one ``insert_text`` call per printed cell, so PyMuPDF ``page.get_text()`` returns one
line per cell in insertion order, the way the real statements come out. Every name, number, merchant
and amount the generators print is invented.
"""

from __future__ import annotations

import json
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalan.amounts import COMMA_DECIMAL, add_months, format_minor  # noqa: E402
from kalan.text import normalize_merchant  # noqa: E402

PAGE_W, PAGE_H = 595, 842
TOP, BOTTOM = 40, 780
LINE = 11
FONT_SIZE = 7.5
MONTHS_TR = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
             "Aralık")
WEEKDAYS_TR = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
MERCHANT_KINDS = ("purchase", "installment", "refund")


def money(minor: int) -> str:
    """``1.234,56``; negative values get a leading minus."""
    return format_minor(minor, COMMA_DECIMAL)


def tr_date(d: date, pad: bool = False) -> str:
    return f"{d.day:02d} {MONTHS_TR[d.month - 1]} {d.year}" if pad else f"{d.day} {MONTHS_TR[d.month - 1]} {d.year}"


def month_date(month: str, day: int) -> date:
    y, m = (int(p) for p in month.split("-"))
    return date(y, m, min(day, monthrange(y, m)[1]))


def shift_month(d: date, n: int) -> date:
    return month_date(add_months(f"{d.year:04d}-{d.month:02d}", n), d.day)


def merchant_of(description: str, kind: str) -> str | None:
    return normalize_merchant(description) if kind in MERCHANT_KINDS else None


@dataclass
class Plan:
    """An installment purchase: ``total`` split into ``n`` parts; the first part takes the rounding."""

    key: str
    description: str
    total: int
    n: int
    start: int  # statement index of the first installment
    day: int = 5  # day of month the purchase was made

    @property
    def each(self) -> int:
        return self.total // self.n

    def k_at(self, index: int) -> int | None:
        k = index - self.start + 1
        return k if 1 <= k <= self.n else None

    def amount(self, k: int) -> int:
        return self.total - self.each * (self.n - 1) if k == 1 else self.each

    def remaining_after(self, k: int) -> tuple[int, int]:
        """(count, total) still to come after installment ``k``."""
        return self.n - k, self.each * (self.n - k)


class Layout:
    """Positioned text per page; a page is closed with its footer before the next one starts."""

    def __init__(self, footer=None) -> None:
        self.pages: list[list[tuple[float, float, str]]] = [[]]
        self.y = TOP
        self.footer = footer  # callable(layout) -> None, prints the page footer

    def put(self, x: float, text: str, y: float | None = None) -> None:
        self.pages[-1].append((x, self.y if y is None else y, text))

    def line(self, *cells: tuple[float, str], gap: float = LINE) -> None:
        if self.y + gap > BOTTOM + LINE:
            raise OverflowError("page full")
        for x, text in cells:
            self.put(x, text)
        self.y += gap

    def fits(self, n_lines: int) -> bool:
        return self.y + n_lines * LINE <= BOTTOM

    def close_page(self) -> None:
        if self.footer is not None:
            self.footer(self)

    def new_page(self) -> None:
        self.close_page()
        self.pages.append([])
        self.y = TOP


def render(lay: Layout, path: Path, font: pymupdf.Font, producer: str) -> None:
    doc = pymupdf.open()
    for items in lay.pages:
        page = doc.new_page(width=PAGE_W, height=PAGE_H)
        page.insert_font(fontname="F0", fontbuffer=font.buffer)
        for x, y, text in items:
            page.insert_text((x, y), text, fontname="F0", fontsize=FONT_SIZE)
    doc.set_metadata({"title": "Synthetic statement", "producer": producer,
                      "creationDate": "D:20261125000000", "modDate": "D:20261125000000"})
    doc.subset_fonts()
    doc.save(path, garbage=4, deflate=True, no_new_id=True)
    doc.close()


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def installment_truth(plan: Plan, k: int, each_override: int | None = None) -> dict:
    count, _ = plan.remaining_after(k)
    return {
        "remaining_count": count,
        "remaining_amount_each": 0 if count == 0 else (each_override if each_override is not None else plan.each),
        "is_last": k == plan.n,
        "index": k,
        "total_count": plan.n,
        "component": None,
        "plan_key": plan.key,
    }


def open_plans(plans: list[Plan], index: int) -> dict[str, dict]:
    """Plans still running after statement ``index``: key -> remaining count and amount each."""
    out = {}
    for p in plans:
        k = p.k_at(index)
        if k is None and index >= p.start + p.n:
            continue
        if k is None:  # not started yet
            continue
        count, _ = p.remaining_after(k)
        if count:
            out[p.key] = {"remaining_count": count, "amount_each": p.each}
    return out
