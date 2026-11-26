"""Plain sentences the API writes itself (forecast notes, missing statements, suggestions), Turkish and English.

The front end gets the key and the parameters next to every text, so it may write its own (Spanish, Portuguese).
Any other locale falls back to English. Amounts in texts are formatted here from minor units.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TextLocale = str

MONTHS = {
    "tr": ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
           "Aralık"],
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"],
}
CURRENCY_WORD = {"tr": {"TRY": "TL"}, "en": {"TRY": "TL"}}

TEXTS: dict[str, dict[str, str]] = {
    "forecast.note.usual": {
        "tr": "Taksitler ve düzenli ödemeler belli. Diğer harcamalar için bu kartla son {months} ayda genelde "
              "harcadığın tutarı aldım.",
        "en": "Installments and regular payments are known. For other spending I used what you usually spent "
              "with this card over the last {months} months.",
    },
    "forecast.note.one_month": {
        "tr": "Taksitler ve düzenli ödemeler belli. Diğer harcamalar için bu kartın son ekstresindeki tutarı aldım.",
        "en": "Installments and regular payments are known. For other spending I used this card's last statement.",
    },
    "forecast.note.no_history": {
        "tr": "Bu kartın önceki ekstresi yok; tahmin yalnız taksitlerden ve düzenli ödemelerden.",
        "en": "There is no earlier statement of this card, so the forecast only counts installments and regular "
              "payments.",
    },
    "alert.missing_statement.one": {
        "tr": "{card} için {range} ekstresi yok. Bankanın uygulamasından PDF olarak indirip ekle.",
        "en": "The {range} statement of {card} is missing. Download it as a PDF from your bank's app and add it.",
    },
    "alert.missing_statement.many": {
        "tr": "{card} için {range} ekstreleri yok. Bankanın uygulamasından PDF olarak indirip ekle.",
        "en": "The {range} statements of {card} are missing. Download them as PDFs from your bank's app and add "
              "them.",
    },
    "suggestion.duplicate_service.title": {
        "tr": "Aynı işi yapan {count} aboneliğin var",
        "en": "{count} subscriptions do the same job",
    },
    "suggestion.duplicate_service.body": {
        "tr": "{merchants} aynı işi görüyor ({group}). Birini kapatırsan ayda en az {saving} kalır.",
        "en": "{merchants} do the same job ({group}). Dropping one keeps at least {saving} a month.",
    },
    "suggestion.price_rise.title": {
        "tr": "{merchant} zam yaptı",
        "en": "{merchant} raised its price",
    },
    "suggestion.price_rise.body": {
        "tr": "{merchant} {amount_previous} iken {amount_last} oldu. Kullanmıyorsan iptal et ya da daha ucuz bir "
              "plana geç.",
        "en": "{merchant} went from {amount_previous} to {amount_last}. Cancel it if you do not use it, or move to a "
              "cheaper plan.",
    },
    "suggestion.card_fee.title": {
        "tr": "{card} için {amount} kart aidatı ödedin",
        "en": "You paid a {amount} card fee on {card}",
    },
    "suggestion.card_fee.body": {
        "tr": "Bankadan aidatın iadesini isteyebilir ya da aidatsız bir karta geçebilirsin. Haklarım bölümünde "
              "dilekçe hazır.",
        "en": "You can ask the bank to refund it or move to a card without a fee. Your rights page has a letter "
              "ready.",
    },
    "suggestion.interest.title": {
        "tr": "{card} ekstresinde {amount} faiz ve gecikme ücreti var",
        "en": "{card} charged {amount} in interest and late fees",
    },
    "suggestion.interest.body": {
        "tr": "Ekstrenin tamamını son ödeme gününe kadar ödersen bu tutar çıkmaz.",
        "en": "Paying the full statement by its due date keeps this amount off your next statement.",
    },
    "suggestion.foreign_currency.title": {
        "tr": "Döviz harcaman ayda ortalama {monthly_average}",
        "en": "You spend about {monthly_average} a month in foreign currency",
    },
    "suggestion.foreign_currency.body": {
        "tr": "Bankalar döviz işlemlerinde kur farkı ve komisyon alır ({currencies}). Komisyonsuz ya da dövizli "
              "bir kart bunu azaltır.",
        "en": "Banks add an exchange margin and fees on these ({currencies}). A card without foreign fees, or a "
              "currency account, lowers it.",
    },
    "suggestion.frequent_place.title": {
        "tr": "{merchant} için ayda ortalama {monthly_average} harcıyorsun",
        "en": "You spend about {monthly_average} a month at {merchant}",
    },
    "suggestion.frequent_place.body": {
        "tr": "Bu yarıya inse yılda {yearly} kalır.",
        "en": "Halving it would keep {yearly} a year.",
    },
    "suggestion.installment_relief.title": {
        "tr": "{month_name} taksitlerin {amount} azalıyor",
        "en": "Your installments drop by {amount} in {month_name}",
    },
    "suggestion.installment_relief.body": {
        "tr": "{plans} taksit planı bitiyor. O aydan sonra bu tutarı birikime ayırabilirsin.",
        "en": "{plans} installment plans end. From then on you could put this amount aside.",
    },
}


def lang(locale: str | None) -> str:
    return "tr" if locale == "tr" else "en"


def month_name(month: str, locale: str | None, with_year: bool = True) -> str:
    y, m = month.split("-")
    name = MONTHS[lang(locale)][int(m) - 1]
    return f"{name} {y}" if with_year else name


def month_range(first: str, last: str, locale: str | None) -> str:
    """``Mart 2026``, ``Mart–Ağustos 2026``, ``Kasım 2025–Şubat 2026``."""
    if first == last:
        return month_name(first, locale)
    if first[:4] == last[:4]:
        return f"{month_name(first, locale, with_year=False)}–{month_name(last, locale)}"
    return f"{month_name(first, locale)}–{month_name(last, locale)}"


def money(amount: int, currency: str, locale: str | None) -> str:
    """``1.234,56 TL`` (tr) / ``1,234.56 TL`` (en); whole amounts drop the decimals."""
    value = (Decimal(amount) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, frac = f"{value:,.2f}".split(".")
    if lang(locale) == "tr":
        whole = whole.replace(",", ".")
        number = whole if frac == "00" else f"{whole},{frac}"
    else:
        number = whole if frac == "00" else f"{whole}.{frac}"
    word = CURRENCY_WORD[lang(locale)].get(currency, currency)
    return f"{number} {word}"


def text(key: str, locale: str | None, **params: object) -> str:
    entry = TEXTS[key]
    return entry[lang(locale)].format(**params)


__all__ = ["TEXTS", "lang", "money", "month_name", "month_range", "text"]
