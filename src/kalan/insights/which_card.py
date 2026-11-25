""""Which card should I use for installments today?"

For each card (bank + last four digits) the latest statement gives the cut-off day of the month and the
gap between cut-off and due date. A purchase made today is billed on the first cut-off on or after
today; its first installment is due on that statement's due date. When the due date falls on a weekend
or a public holiday, payment on the next business day is assumed to be on time. The card whose first
installment is due latest gives the most interest-free days and wins (a tie goes to the later cut-off).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from kalan.alerts import latest_per_card
from kalan.insights.dates import holiday_years, next_business_day, next_cut_offs
from kalan.models import Statement

STALE_DAYS = 100


def _next_billing(st: Statement, today: date) -> tuple[date, date, str]:
    """``(cut_off, due_date, basis)`` of the first statement on or after ``today`` for this card."""
    offset = (st.due_date - st.period_end).days
    if today <= st.period_end:
        return st.period_end, st.due_date, "printed"
    months = (today.year - st.period_end.year) * 12 + today.month - st.period_end.month + 2
    for cut in next_cut_offs(st.period_end, st.next_period_end, months):
        if cut >= today:
            if cut == st.next_period_end and st.next_due_date is not None:
                return cut, st.next_due_date, "printed"
            return cut, cut + timedelta(days=offset), "printed" if cut == st.next_period_end else "projected"
    raise AssertionError("a cut-off on or after today always exists")


def card_options(statements: list[Statement], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    cards: list[dict[str, Any]] = []
    years_unknown: set[int] = set()
    for st in latest_per_card(statements):
        cut, due, basis = _next_billing(st, today)
        effective = next_business_day(due)
        if due.year not in holiday_years():
            years_unknown.add(due.year)
        cards.append({
            "bank": st.bank, "product": st.product, "card_last4": st.card_last4, "statement_id": st.id,
            "cut_off": cut.isoformat(), "cut_off_day": st.period_end.day,
            "due_offset_days": (st.due_date - st.period_end).days, "due_date": due.isoformat(),
            "due_date_effective": effective.isoformat(), "shifted_days": (effective - due).days,
            "interest_free_days": (effective - today).days, "days_until_cut_off": (cut - today).days,
            "first_installment_month": f"{cut.year:04d}-{cut.month:02d}", "basis": basis,
            "stale": (today - st.period_end).days > STALE_DAYS,
        })
    cards.sort(key=lambda c: (-c["interest_free_days"], c["cut_off"], c["bank"], c["card_last4"] or ""))
    assumptions = [
        {"key": "cut_off_day_included", "text": "A purchase on the cut-off day is billed on that statement; a "
                                                "late posting by the merchant may move it to the next one."},
        {"key": "day_and_gap_kept", "text": "Later cut-offs keep the last statement's day of the month (clipped in "
                                            "short months) and the same number of days to the due date, unless the "
                                            "statement prints the next dates."},
        {"key": "due_on_next_business_day", "text": "A due date on a weekend or public holiday is paid on the next "
                                                    "business day without interest."},
    ]
    if years_unknown:
        assumptions.append({"key": "holidays_unknown", "text": "Religious holidays are not in the data for "
                                                               f"{', '.join(str(y) for y in sorted(years_unknown))}; "
                                                               "only weekends and fixed holidays were checked."})
    if any(c["stale"] for c in cards):
        assumptions.append({"key": "stale_statement", "text": "Some cards have no statement in the last "
                                                              f"{STALE_DAYS} days; their dates are projected."})
    return {"date": today.isoformat(), "best": cards[0] if cards else None, "cards": cards,
            "assumptions": assumptions}
