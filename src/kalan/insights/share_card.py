"""Shareable installment load card: the next 12 months as one sentence and a self-contained SVG.

Nothing personal goes in: no merchant names, no plan descriptions, no card numbers, no bank names.
Amounts are off by default; then only the shape (each month relative to the busiest one) and the
month names are shown. The SVG uses only system fonts and no external resources.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from xml.sax.saxutils import escape

from kalan.amounts import months_between
from kalan.schedule import Schedule

CardLocale = Literal["tr", "en"]

_MONTHS = {
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
           "Aralık"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
}
_TITLE = {"tr": "Taksit yükü, önümüzdeki {n} ay", "en": "Installment load, next {n} months"}
_FOOT = {"tr": "kalan ile hesaplandı", "en": "computed with kalan"}

# Card palette (self-contained image, so the values live here rather than in a front-end theme).
_W, _H = 1200, 630
_BG, _INK, _MUTED, _BAR, _PEAK, _RULE = "#F4F1EA", "#1D1C1A", "#77726A", "#2E4A3D", "#B8502E", "#D9D3C7"
_FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"


def _month_name(month: str, locale: CardLocale, short: bool = False) -> str:
    y, m = (int(p) for p in month.split("-"))
    name = _MONTHS[locale][m - 1]
    return name[:3] if short else f"{name} {y}"


def _money(minor: int, currency: str) -> str:
    whole = int((Decimal(minor) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return f"{whole:,}".replace(",", ".") + f" {currency}"


def load_sentence(schedule: Schedule, locale: CardLocale = "tr", show_amounts: bool = False) -> str:
    months = schedule.months
    totals = [m.total for m in months]
    n = len(months)
    if not months or max(totals) == 0:
        return {"tr": f"Önümüzdeki {n} ayda taksit yükü yok.",
                "en": f"No installment load in the next {n} months."}[locale]
    peak_i = max(range(n), key=lambda i: (totals[i], -i))
    peak = months[peak_i].month
    half = next((months[i].month for i in range(peak_i + 1, n) if totals[i] * 2 <= totals[peak_i]), None)
    last = months[-1].month
    plans = len(schedule.plans)
    ending = sum(1 for p in schedule.plans if months_between(p.ends, last) >= 0)
    if locale == "tr":
        out = f"Önümüzdeki {n} ayda taksit yükü en yüksek {_month_name(peak, locale)} ayında"
        if show_amounts:
            out += f" ({_money(totals[peak_i], schedule.currency)})"
        out += (f", {_month_name(half, locale)} itibarıyla yarının altına iniyor" if half
                else ", dönem boyunca yarının altına inmiyor")
        return out + f"; {plans} planın {ending} tanesi bu sürede bitiyor."
    out = f"Over the next {n} months the installment load peaks in {_month_name(peak, locale)}"
    if show_amounts:
        out += f" ({_money(totals[peak_i], schedule.currency)})"
    out += (f", falls below half by {_month_name(half, locale)}" if half else ", and stays above half throughout")
    return out + f"; {ending} of {plans} plans end within this time."


def load_svg(schedule: Schedule, locale: CardLocale = "tr", show_amounts: bool = False) -> str:
    months = schedule.months
    totals = [m.total for m in months]
    top = max(totals, default=0)
    peak_i = max(range(len(totals)), key=lambda i: (totals[i], -i)) if top else -1
    left, right, base, chart_h = 80, _W - 80, 520, 300
    slot = (right - left) / max(len(months), 1)
    bar_w = slot * 0.62
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" viewBox="0 0 {_W} {_H}" '
        f'role="img" aria-label="{escape(load_sentence(schedule, locale, show_amounts), {chr(34): "&quot;"})}">',
        f'<rect width="{_W}" height="{_H}" fill="{_BG}"/>',
        f'<text x="{left}" y="110" font-family="{_FONT}" font-size="44" font-weight="600" fill="{_INK}">'
        f'{escape(_TITLE[locale].format(n=len(months)))}</text>',
        f'<line x1="{left}" y1="{base}" x2="{right}" y2="{base}" stroke="{_RULE}" stroke-width="2"/>',
    ]
    for i, m in enumerate(months):
        x = left + i * slot + (slot - bar_w) / 2
        h = 0 if top == 0 else max(round(chart_h * m.total / top), 4 if m.total else 0)
        color = _PEAK if i == peak_i else _BAR
        if h:
            parts.append(f'<rect x="{x:.1f}" y="{base - h}" width="{bar_w:.1f}" height="{h}" rx="6" fill="{color}"/>')
        cx = x + bar_w / 2
        parts.append(f'<text x="{cx:.1f}" y="{base + 38}" text-anchor="middle" font-family="{_FONT}" '
                     f'font-size="22" fill="{_MUTED}">{escape(_month_name(m.month, locale, short=True))}</text>')
        if show_amounts and m.total:
            parts.append(f'<text x="{cx:.1f}" y="{base - h - 12}" text-anchor="middle" font-family="{_FONT}" '
                         f'font-size="18" fill="{_INK}">{escape(_money(m.total, schedule.currency).split()[0])}</text>')
    if months:
        span = f"{_month_name(months[0].month, locale)} – {_month_name(months[-1].month, locale)}"
        parts.append(f'<text x="{left}" y="158" font-family="{_FONT}" font-size="26" fill="{_MUTED}">'
                     f'{escape(span)}</text>')
    parts.append(f'<text x="{right}" y="{_H - 30}" text-anchor="end" font-family="{_FONT}" font-size="20" '
                 f'fill="{_MUTED}">{escape(_FOOT[locale])}</text>')
    parts.append("</svg>")
    return "".join(parts)


def load_card(schedule: Schedule, locale: CardLocale = "tr", show_amounts: bool = False) -> dict[str, Any]:
    """Sentence, SVG and the month shape (``share`` of the busiest month, two decimals)."""
    top = max((m.total for m in schedule.months), default=0)
    shape = []
    for m in schedule.months:
        share = "0.00" if top == 0 else str((Decimal(m.total) / Decimal(top)).quantize(Decimal("0.01"),
                                                                                        rounding=ROUND_HALF_UP))
        row: dict[str, Any] = {"month": m.month, "share": share}
        if show_amounts:
            row["total"] = m.total
        shape.append(row)
    out: dict[str, Any] = {
        "locale": locale,
        "show_amounts": show_amounts,
        "sentence": load_sentence(schedule, locale, show_amounts),
        "months": shape,
        "svg": load_svg(schedule, locale, show_amounts),
    }
    if show_amounts:
        out["currency"] = schedule.currency
    return out
