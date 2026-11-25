"""Save gate: checks a parsed statement before it is stored and names what is wrong with a code.

``hard`` findings block the save (the read is not usable: no rows, impossible dates, amounts read at the wrong
scale, a statement already stored). ``soft`` findings save the statement with ``needs_review: true`` and the
findings in ``review``. Codes are listed in ``docs/api.md``. Pure functions: the caller passes the statements
already stored.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import NamedTuple

from kalan.models import GateIssue, Statement

SCALES = (10, 100, 1000)
PERIOD_DAYS = (20, 45)  # a usual statement period is about a month
LATE_ROW_DAYS = 3  # rows dated after the cut-off by more than this
CYCLE_DAYS = 30.44  # average length of a monthly billing cycle
MAX_CYCLE_GAP = 3  # compare installments with the latest stored statement at most this many cycles back


def _hard(code: str, message: str, **details) -> GateIssue:
    return GateIssue(code=code, severity="hard", message=message, details=details)


def _soft(code: str, message: str, **details) -> GateIssue:
    return GateIssue(code=code, severity="soft", message=message, details=details)


def _scaled(expected: int, computed: int) -> int | None:
    """The factor (10, 100, 1000) by which one value is the other, or None."""
    if expected == 0 or computed == 0 or expected == computed:
        return None
    big, small = max(abs(expected), abs(computed)), min(abs(expected), abs(computed))
    for f in SCALES:
        if abs(big - small * f) <= f:  # rounding of the smaller value
            return f
    return None


def _same_card(a: Statement, b: Statement) -> bool:
    return a.bank == b.bank and a.card_last4 == b.card_last4


def _duplicate(st: Statement, stored: Iterable[Statement]) -> Statement | None:
    for other in stored:
        if other.id == st.id or (
            _same_card(st, other) and other.period_start == st.period_start and other.period_end == st.period_end
        ):
            return other
    return None


def _cycles_between(earlier: Statement, later: Statement) -> int:
    """Billing cycles from ``earlier`` to ``later``: 1 for the adjacent statement, 2 when one is missing.

    Counted from the cut-off dates, rounded, so a cut-off moved by a weekend or holiday does not change it.
    """
    return round((later.period_end - earlier.period_end).days / CYCLE_DAYS)


def _previous(st: Statement, stored: Iterable[Statement]) -> tuple[Statement, int] | None:
    """The latest stored statement of the same card before ``st`` and the cycles between them.

    Statements can be uploaded in any order, so the latest stored one may be several cycles back; it is
    used when it is at most :data:`MAX_CYCLE_GAP` cycles back.
    """
    earlier = [o for o in stored if _same_card(st, o) and o.period_end < st.period_start]
    prev = max(earlier, key=lambda o: o.period_end, default=None)
    if prev is None:
        return None
    cycles = _cycles_between(prev, st)
    return (prev, cycles) if 1 <= cycles <= MAX_CYCLE_GAP else None


class _Plan(NamedTuple):
    low: int | None  # lowest installment number printed
    high: int | None  # highest installment number printed
    remaining: int  # installments still to come after this statement
    closed: bool  # last installment or early payoff in this statement


def _plan_index(st: Statement) -> dict[str, _Plan]:
    """plan id -> what this statement prints of the plan.

    An early payoff prints the remaining installments together, so a statement can hold several numbers.
    """
    out: dict[str, _Plan] = {}
    for t in st.transactions:
        info = t.installment
        if info is None:
            continue
        low, high, remaining, closed = out.get(info.plan_id, (None, None, info.remaining_count, False))
        if info.index is not None:
            low = info.index if low is None else min(low, info.index)
            high = info.index if high is None else max(high, info.index)
        out[info.plan_id] = _Plan(low, high, min(remaining, info.remaining_count),
                                  closed or info.is_last or info.remaining_count == 0)
    return out


def check(st: Statement, stored: Iterable[Statement] = ()) -> list[GateIssue]:
    """Every finding for ``st``; ``stored`` are the statements already saved."""
    stored = list(stored)
    issues: list[GateIssue] = []

    dup = _duplicate(st, stored)
    if dup is not None:
        issues.append(_hard("duplicate_statement", "This statement is already stored.", statement_id=dup.id))

    if st.period_start > st.period_end:
        issues.append(_hard("period_invalid", "The period starts after it ends.",
                            period_start=st.period_start.isoformat(), period_end=st.period_end.isoformat()))
    elif st.due_date < st.period_end:
        issues.append(_hard("due_before_cut_off", "The due date is before the cut-off date.",
                            period_end=st.period_end.isoformat(), due_date=st.due_date.isoformat()))
    else:
        days = (st.period_end - st.period_start).days + 1
        if not PERIOD_DAYS[0] <= days <= PERIOD_DAYS[1]:
            issues.append(_soft("period_unusual", f"The period is {days} days long.", days=days))

    if not st.transactions and st.statement_total != 0:
        issues.append(_hard("no_transactions", "No rows were read, but the statement total is not zero."))

    rec = st.reconciliation
    if rec is None:
        issues.append(_soft("not_reconciled", "The statement was not checked against its totals."))
    elif rec.status != "ok":
        scaled = [(c.name, f) for c in rec.checks if c.status != "ok" and (f := _scaled(c.expected, c.computed))]
        if scaled:
            issues.append(_hard("amount_scale", "Amounts were read at the wrong scale (decimal separator).",
                                checks=[name for name, _ in scaled], factor=scaled[0][1]))
        issues.append(_soft("reconciliation_mismatch", "The rows do not add up to the printed totals.",
                            difference=rec.difference, checks=[c.name for c in rec.checks if c.status != "ok"]))

    if st.printed_summary is None and st.transactions:
        issues.append(_soft("no_printed_summary", "No printed totals block was found; only the total was checked."))

    late = [t for t in st.transactions if t.date > st.period_end + timedelta(days=LATE_ROW_DAYS)]
    if late:
        issues.append(_soft("rows_after_cut_off", "Some rows are dated after the cut-off date.", count=len(late)))

    if st.minimum_payment > st.statement_total > 0:
        issues.append(_soft("minimum_above_total", "The minimum payment is larger than the statement total."))

    bad_rows = []
    for t in st.transactions:
        info = t.installment
        if info is None:
            continue
        wrong = (info.is_last and info.remaining_count > 0) or (
            info.index is not None and info.total_count is not None
            and (info.index > info.total_count or info.index + info.remaining_count != info.total_count)
        )
        if wrong:
            bad_rows.append(t.id)
    if bad_rows:
        issues.append(_soft("installment_counter", "Installment number, count and remaining count disagree.",
                            transaction_ids=bad_rows))

    found = _previous(st, stored)
    if found is not None:
        prev, cycles = found
        now, before = _plan_index(st), _plan_index(prev)
        # ``cycles`` statements later the plan is ``cycles`` installments further on
        jumped = sorted(p for p, plan in now.items()
                        if p in before and plan.low is not None and before[p].high is not None
                        and plan.low != before[p].high + cycles)
        # a plan with fewer installments left than cycles passed ended in a statement not stored
        missing = sorted(p for p, plan in before.items()
                         if not plan.closed and plan.remaining >= cycles and p not in now)
        if jumped:
            issues.append(_soft("installment_sequence", "An installment number does not follow the previous "
                                "statement's.", plan_ids=jumped, previous_statement_id=prev.id, cycles=cycles))
        if missing:
            issues.append(_soft("installment_missing", "A plan still running in the previous statement is not in "
                                "this one (paid off early, or a row was not read).", plan_ids=missing,
                                previous_statement_id=prev.id, cycles=cycles))
    return issues


def hard(issues: Iterable[GateIssue]) -> list[GateIssue]:
    return [i for i in issues if i.severity == "hard"]


def soft(issues: Iterable[GateIssue]) -> list[GateIssue]:
    return [i for i in issues if i.severity == "soft"]
