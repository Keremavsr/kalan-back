"""Calendar helpers: cut-off dates clipped to short months, weekends and Turkish public holidays."""

from __future__ import annotations

import calendar
import json
from datetime import date, timedelta
from functools import cache
from importlib import resources


def clip_day(year: int, month: int, day: int) -> date:
    """``day`` of the month, clipped to the month's length (31 -> 30 November, 28/29 February)."""
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def shift_month(d: date, n: int, day: int | None = None) -> date:
    """Same day ``n`` months later (``day`` overrides the day kept), clipped to the month's length."""
    idx = d.year * 12 + d.month - 1 + n
    return clip_day(idx // 12, idx % 12 + 1, day or d.day)


def next_cut_offs(period_end: date, next_period_end: date | None, count: int) -> list[date]:
    """The ``count`` cut-off dates after ``period_end``. A printed next cut-off is used as the first one;
    later ones keep ``period_end``'s day of the month."""
    out: list[date] = []
    for i in range(1, count + 1):
        if i == 1 and next_period_end is not None and next_period_end > period_end:
            out.append(next_period_end)
        else:
            out.append(shift_month(period_end, i))
    return out


@cache
def _holidays() -> dict[str, object]:
    raw = resources.files("kalan.insights.data").joinpath("tr_holidays.json").read_text(encoding="utf-8")
    return json.loads(raw)


def holiday_years() -> set[int]:
    """Years whose moving (religious) holidays are in the data file."""
    return {int(y) for y in _holidays()["religious"]}  # type: ignore[union-attr]


def holidays(year: int) -> set[date]:
    """Full-day public holidays in Turkey (half days such as arife count as working days)."""
    data = _holidays()
    out = {date(year, int(md[:2]), int(md[3:])) for md in data["fixed"]}  # type: ignore[union-attr]
    for iso in data["religious"].get(str(year), []):  # type: ignore[union-attr]
        out.add(date.fromisoformat(iso))
    return out


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays(d.year)


def next_business_day(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d
