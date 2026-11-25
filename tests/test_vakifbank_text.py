"""VakıfBank parser on hand-written synthetic text (invented merchants and amounts)."""

from datetime import date

import pytest

from kalan.parsers import ParseError, detect_parser
from kalan.parsers.tr_vakifbank_worldcard import VakifbankWorldcard

HEADER = """Kredi Kartı Hesap Özeti (TL)
VakıfBank Worldcard ile otomatik ödeme talimatı verin.
HESAP BİLGİLERİNİZ
Dönem Borcunuz
:
{total} TL
Asgari Ödeme Tutarı
:
{minimum} TL
Son Ödeme Tarihi
:
04.05.2026
Kart No
:
[CARD ****0000]
Limitiniz
:
50,000.00 TL
Hesap Kesim Tarihi
:
24.04.2026
Bir Sonraki Hesap Kesim Tarihi
:
22.05.2026
Bir Sonraki Son Ödeme Tarihi
:
01.06.2026
Sayın [NAME]
[ADDRESS]
VakıfBank Worldcard'ınızla tüm işlemlerinizi yapabilirsiniz.
İŞLEM TARİHİ
AÇIKLAMA
TUTAR (TL)
KALAN TAKSİT
WORLDPUAN
"""

FOOTER = """HESAP ÖZETİ
Önceki Hesap Bakiyesi
{prev}
Dönem Içi Islemler
{period}
Toplam Faiz ve Ücretler
{fees}
Ödemeler
{payments}
Dönem Borcunuz
{total}
WORLDPUAN BILGILERINIZ
Sayfa 1 / 1
FAİZ ORANLARI
AYLIK
YILLIK
Alışveriş Faiz Oranı
%3.11    %37.32
Nakit Çekim Faiz Oranı
%2.87    %34.44
"""

ROWS = """23.03.2026
ÖNCEKİ DÖNEM HESAP ÖZETİ BAKİYENİZ
1,000.00
26.03.2026
ÖDEMENİZ İÇİN TEŞEKKÜRLER
+1,000.00
28.03.2026
MARKET-ORNEK ISTANBUL
250.50
4.12
30.03.2026
ELEKTRO-ORNEK 2/6
500.00
4x500.00
02.04.2026
Mobil-Taksitli Nakit Avans 1.Taksit Anap
900.00
1x950.00
02.04.2026
Mobil-Taksitli Nakit Avans 1.BSMV
10.00
1x5.00
02.04.2026
Mobil-Taksitli Nakit Avans 1.Vade Farkı
60.00
1x40.00
05.04.2026
Mobil-Taksitli Nakit Avans 2.Taksit Anap
950.00
Son Taksit
05.04.2026
Erken Kapama Faiz İptal
+40.00
08.04.2026
STREAMPLAY-ORNEK LONDON GB 9.99 GBP
430.00
"""


def _text(rows=ROWS, total="3,060.50", period="2,990.50", fees="70.00", payments="+1,000.00", prev="1,000.00"):
    # period = 250.50 + 500 + 900 + 950 - 40 + 430 = 2,990.50 ; fees = 10 + 60 = 70.00
    # total = 1,000 (previous) + 2,990.50 + 70 - 1,000 (payment) = 3,060.50
    head = HEADER.format(total=total, minimum="412.10")
    foot = FOOTER.format(prev=prev, period=period, fees=fees, payments=payments, total=total)
    return head + rows + foot


def test_detect():
    assert isinstance(detect_parser(_text()), VakifbankWorldcard)


def test_header_fields():
    st = VakifbankWorldcard().parse(_text())
    assert st.card_last4 == "0000"
    assert st.period_end == date(2026, 4, 24)
    assert st.period_start == date(2026, 3, 24)
    assert st.due_date == date(2026, 5, 4)
    assert st.statement_total == 306050
    assert st.minimum_payment == 41210
    assert st.credit_limit == 5000000
    assert st.next_period_end == date(2026, 5, 22)
    assert st.interest_rates == {
        "purchase_monthly": "3.11", "purchase_yearly": "37.32",
        "cash_advance_monthly": "2.87", "cash_advance_yearly": "34.44",
    }
    assert st.printed_summary.payments == 100000
    assert st.printed_summary.period_transactions == 299050


def test_rows():
    st = VakifbankWorldcard().parse(_text())
    kinds = [t.kind for t in st.transactions]
    assert kinds == [
        "carry_over", "payment", "purchase", "installment", "cash_advance", "fee", "interest", "cash_advance",
        "refund", "purchase",
    ]
    market = st.transactions[2]
    assert market.points == "4.12" and market.amount == 25050 and market.merchant == "MARKET-ORNEK"
    inst = st.transactions[3].installment
    assert (inst.index, inst.total_count, inst.remaining_count, inst.remaining_amount_each) == (2, 6, 4, 50000)
    assert st.transactions[3].merchant == "ELEKTRO-ORNEK"
    fx = st.transactions[9]
    assert (fx.original_amount, fx.original_currency) == (999, "GBP")


def test_cash_advance_grouped_and_closed():
    st = VakifbankWorldcard().parse(_text())
    ca = [t for t in st.transactions if t.installment and t.installment.component]
    plan_ids = {t.installment.plan_id for t in ca}
    assert len(plan_ids) == 1
    components = [t.installment.component for t in ca]
    assert components == ["principal", "bsmv", "interest", "principal", "early_payoff"]
    payoff = ca[-1].installment
    assert payoff.remaining_count == 0 and payoff.is_last


def test_broken_layout_raises():
    with pytest.raises(ParseError):
        VakifbankWorldcard().parse(_text(rows="28.03.2026\nNO AMOUNT HERE\n"))
