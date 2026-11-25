from datetime import date

from kalan.models import InstallmentInfo, ManualInstallment, Statement, Transaction
from kalan.schedule import build_schedule, plan_description, plan_states


def _tx(n, desc, amount, info=None, kind="installment", d=date(2026, 9, 10)):
    return Transaction(id=f"tx_{n}", date=d, description=desc, amount=amount, direction="debit", kind=kind,
                       installment=info)


def _info(plan, remaining, each, index=None, total=None, last=False, component=None):
    return InstallmentInfo(remaining_count=remaining, remaining_amount_each=each, is_last=last, plan_id=plan,
                           index=index, total_count=total, component=component)


def _st(end, txs, sid=None):
    return Statement(id=sid or f"st_{end.isoformat()}", bank="vakifbank", product="Worldcard", card_last4="0000",
                     period_start=date(end.year, end.month, 1), period_end=end, due_date=end,
                     statement_total=0, minimum_payment=0, transactions=txs)


def test_plan_description_strips_markers():
    assert plan_description("ELEKTRO-ORNEK 2/6") == "ELEKTRO-ORNEK"
    assert plan_description("Mobil-Taksitli Nakit Avans 1.Taksit Anap") == "Mobil-Taksitli Nakit Avans"
    assert plan_description("Mobil-Taksitli Nakit Avans 2.Vade Farkı") == "Mobil-Taksitli Nakit Avans"
    assert plan_description("MOBILYA-ORNEK 3.Taksit") == "MOBILYA-ORNEK"


def test_purchase_projection_and_latest_wins():
    sep = _st(date(2026, 9, 24), [_tx(1, "ELEKTRO-ORNEK 2/6", 50000, _info("pl_a", 4, 50000, 2, 6))])
    oct_ = _st(date(2026, 10, 23), [_tx(2, "ELEKTRO-ORNEK 3/6", 50000, _info("pl_a", 3, 50000, 3, 6))])
    s = build_schedule([oct_, sep], months=4)
    assert s.start_month == "2026-11"
    assert [m.month for m in s.months] == ["2026-11", "2026-12", "2027-01", "2027-02"]
    assert [m.total for m in s.months] == [50000, 50000, 50000, 0]
    assert [(i.index, i.count) for m in s.months for i in m.items] == [(4, 6), (5, 6), (6, 6)]
    (plan,) = s.plans
    assert (plan.remaining_count, plan.amount_each, plan.ends, plan.total_remaining) == (3, 50000, "2027-01", 150000)
    assert plan.breakdown is None and all(i.breakdown is None for m in s.months for i in m.items)


def test_last_installment_ends_plan():
    st = _st(date(2026, 9, 24), [_tx(1, "ELEKTRO-ORNEK 6/6", 50000, _info("pl_a", 0, 0, 6, 6, last=True))])
    s = build_schedule([st], months=3)
    assert s.plans == [] and all(m.total == 0 for m in s.months)


def test_cash_advance_components_summed():
    rows = [
        _tx(1, "Mobil-Taksitli Nakit Avans 1.Taksit Anap", 90000, _info("pl_c", 2, 95000, 1, 3, component="principal"),
            kind="cash_advance"),
        _tx(2, "Mobil-Taksitli Nakit Avans 1.BSMV", 1000, _info("pl_c", 2, 500, 1, 3, component="bsmv"), kind="fee"),
        _tx(3, "Mobil-Taksitli Nakit Avans 1.KKDF", 1000, _info("pl_c", 2, 500, 1, 3, component="kkdf"), kind="fee"),
        _tx(4, "Mobil-Taksitli Nakit Avans 1.Vade Farkı", 6000, _info("pl_c", 2, 4000, 1, 3, component="interest"),
            kind="interest"),
    ]
    s = build_schedule([_st(date(2026, 9, 24), rows)], months=3)
    (plan,) = s.plans
    assert plan.kind == "cash_advance"
    assert plan.amount_each == 100000
    assert plan.breakdown.model_dump() == {"principal": 190000, "interest": 8000, "bsmv": 1000, "kkdf": 1000}
    assert plan.breakdown.total() == plan.total_remaining
    item = s.months[0].items[0]
    assert item.breakdown.model_dump() == {"principal": 95000, "interest": 4000, "bsmv": 500, "kkdf": 500}
    assert item.breakdown.total() == item.amount
    assert [m.total for m in s.months] == [100000, 100000, 0]
    assert plan.description == "Mobil-Taksitli Nakit Avans"


def test_early_payoff_ends_plan():
    sep = _st(date(2026, 9, 24), [
        _tx(1, "Mobil-Taksitli Nakit Avans 1.Taksit Anap", 90000, _info("pl_c", 2, 95000, 1, 3, component="principal"),
            kind="cash_advance"),
    ])
    oct_ = _st(date(2026, 10, 23), [
        _tx(2, "Mobil-Taksitli Nakit Avans 2.Taksit Anap", 95000, _info("pl_c", 1, 95000, 2, 3, component="principal"),
            kind="cash_advance"),
        _tx(3, "Erken Kapama Faiz İptal", 3000, _info("pl_c", 0, 0, last=True, component="early_payoff"),
            kind="refund"),
    ])
    assert plan_states([sep, oct_])["pl_c"].closed
    assert build_schedule([sep, oct_], months=3).plans == []


def test_plan_missing_from_newer_statement_drops_past_months():
    sep = _st(date(2026, 9, 24), [_tx(1, "ELEKTRO-ORNEK 1/3", 50000, _info("pl_a", 2, 50000, 1, 3))])
    nov = _st(date(2026, 11, 24), [])
    s = build_schedule([sep, nov], months=2)
    assert s.start_month == "2026-12"
    assert s.plans == []  # installments 2 and 3 fell in Oct and Nov


def test_manual_entries_join_projection():
    sep = _st(date(2026, 9, 24), [_tx(1, "ELEKTRO-ORNEK 2/3", 50000, _info("pl_a", 1, 50000, 2, 3))])
    manual = ManualInstallment(id="pl_m1", description="ORNEK KURS", start_date=date(2026, 10, 5), total_count=3,
                               amount_each=20000)
    s = build_schedule([sep], [manual], months=4)
    assert [m.total for m in s.months] == [70000, 20000, 20000, 0]
    m_plan = next(p for p in s.plans if p.plan_id == "pl_m1")
    # window starts 2026-10 (month after the September statement); manual plan bills Oct, Nov, Dec
    assert s.start_month == "2026-10"
    assert (m_plan.source, m_plan.remaining_count, m_plan.ends) == ("manual", 3, "2026-12")
    assert [(i.index, i.count) for i in s.months[0].items if i.plan_id == "pl_m1"] == [(1, 3)]


def test_manual_entry_partly_in_the_past():
    manual = ManualInstallment(id="pl_m1", description="X", start_date=date(2026, 8, 1), total_count=4,
                               amount_each=100)
    s = build_schedule([], [manual], months=3, start_month="2026-10")
    assert [m.total for m in s.months] == [100, 100, 0]
    assert [i.index for m in s.months for i in m.items] == [3, 4]


def test_manual_only_with_cut_off_day():
    manual = ManualInstallment(id="pl_m1", description="X", start_date=date(2026, 10, 28), total_count=2,
                               amount_each=100)
    s = build_schedule([], [manual], months=3, start_month="2026-10", cut_off_day=25)
    assert [m.total for m in s.months] == [0, 100, 100]
