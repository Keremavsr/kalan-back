"""Locale-aware amount and date parsing.

Parsers never guess the number format: each one declares an :class:`AmountFormat`. VakıfBank prints
``2,345.60`` (comma thousands, dot decimal) and marks credits with a leading ``+``; many other Turkish
banks and Brazilian banks print ``2.345,60``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from kalan.text import fold

_SPACES = "    "
_CURRENCY_TOKENS = (
    "R$", "US$", "TL", "TRY", "USD", "EUR", "GBP", "BRL", "MXN", "SGD", "HKD", "CAD", "€", "$", "£", "₺",
)


@dataclass(frozen=True)
class AmountFormat:
    """How a statement writes numbers.

    ``thousands`` may be ``""`` (never grouped) or ``" "`` (any space, including no-break spaces).
    """

    thousands: str
    decimal: str
    decimals: int = 2
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.thousands == self.decimal:
            raise ValueError("thousands and decimal separators must differ")
        dec = re.escape(self.decimal)
        th = f"[{_SPACES}]" if self.thousands == " " else re.escape(self.thousands)
        whole = rf"(?:\d{{1,3}}(?:{th}\d{{3}})+|\d+)" if self.thousands else r"\d+"
        frac = rf"(?:{dec}\d{{{self.decimals}}})?" if self.decimals else ""
        pattern = rf"^(?P<sign>[+-])?(?P<whole>{whole})(?P<frac>{frac})(?P<tsign>-)?$"
        object.__setattr__(self, "_regex", re.compile(pattern))

    @property
    def regex(self) -> re.Pattern[str]:
        return self._regex


DOT_DECIMAL = AmountFormat(thousands=",", decimal=".")
"""``2,345.60``: VakıfBank, US, UK, Singapore, Hong Kong."""

COMMA_DECIMAL = AmountFormat(thousands=".", decimal=",")
"""``2.345,60``: most Turkish banks, Brazil, Netherlands, Germany."""

SPACE_COMMA = AmountFormat(thousands=" ", decimal=",")
"""``2 345,60``: France, French Canada."""


@dataclass(frozen=True)
class ParsedAmount:
    minor: int
    """Absolute value in minor units."""
    sign: str | None
    """``"+"``, ``"-"`` or ``None`` when no sign was printed."""
    currency: str | None = None

    @property
    def signed(self) -> int:
        return -self.minor if self.sign == "-" else self.minor


def _strip_currency(text: str) -> tuple[str, str | None]:
    t = text.strip()
    for token in _CURRENCY_TOKENS:
        if t.endswith(token):
            return t[: -len(token)].strip(), token
        if t.startswith(token):
            return t[len(token) :].strip(), token
        # sign before a leading symbol: "-$12.00" / "+€3"
        if len(t) > 1 and t[0] in "+-" and t[1:].startswith(token):
            return t[0] + t[1 + len(token) :].strip(), token
    return t, None


def parse_amount(text: str, fmt: AmountFormat) -> ParsedAmount:
    """Parse one printed amount into minor units. Raises ``ValueError`` if it does not fit ``fmt``."""
    body, currency = _strip_currency(text)
    body = re.sub(r"^([+-])\s+", r"\1", body.strip())
    m = fmt.regex.match(body)
    if not m:
        raise ValueError(f"not an amount in format {fmt.thousands!r}/{fmt.decimal!r}: {text!r}")
    whole = re.sub(r"\D", "", m.group("whole"))
    frac = m.group("frac")[1:] if m.group("frac") else ""
    frac = frac.ljust(fmt.decimals, "0")
    minor = int(whole) * 10**fmt.decimals + (int(frac) if frac else 0)
    sign = m.group("sign") or ("-" if m.group("tsign") else None)
    return ParsedAmount(minor=minor, sign=sign, currency=currency)


def try_parse_amount(text: str, fmt: AmountFormat) -> ParsedAmount | None:
    try:
        return parse_amount(text, fmt)
    except ValueError:
        return None


def format_minor(minor: int, fmt: AmountFormat = DOT_DECIMAL) -> str:
    """Inverse of :func:`parse_amount` (used by the synthetic generator)."""
    neg = minor < 0
    whole, frac = divmod(abs(minor), 10**fmt.decimals)
    digits = f"{whole:,}".replace(",", fmt.thousands)
    out = f"{digits}{fmt.decimal}{frac:0{fmt.decimals}d}" if fmt.decimals else digits
    return f"-{out}" if neg else out


# ---------------------------------------------------------------------------------------------- dates

_MONTHS: dict[str, int] = {}
for _num, _names in enumerate(
    [
        "ocak january janeiro enero janvier januari janv",
        "subat february fevereiro febrero fevrier februari fevr",
        "mart march marco marzo mars maart mrt",
        "nisan april abril avril",
        "mayis may mayo mai mei maio",
        "haziran june junho junio juin juni",
        "temmuz july julho julio juillet juli juil",
        "agustos august agosto aout augustus",
        "eylul september setembro septiembre septembre sept set",
        "ekim october outubro octubre octobre oktober okt out",
        "kasim november novembro noviembre novembre",
        "aralik december dezembro diciembre decembre dez dic",
    ],
    start=1,
):
    for _n in _names.split():
        _MONTHS.setdefault(_n, _num)
        _MONTHS.setdefault(_n[:3], _num)


def month_from_name(name: str) -> int:
    key = fold(name).rstrip(".")
    for candidate in (key, key[:3]):
        if candidate in _MONTHS:
            return _MONTHS[candidate]
    raise ValueError(f"unknown month name: {name!r}")


_TOKEN_RE = re.compile(r"YYYY|YY|MMM|MM|M|DD|D")
_TOKEN_PATTERNS = {
    "YYYY": r"(?P<y4>\d{4})",
    "YY": r"(?P<y2>\d{2})",
    "MMM": r"(?P<mon>[^\W\d_]{3,}\.?)",
    "MM": r"(?P<m>\d{2})",
    "M": r"(?P<m>\d{1,2})",
    "DD": r"(?P<d>\d{2})",
    "D": r"(?P<d>\d{1,2})",
}
_DATE_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _date_regex(fmt: str) -> re.Pattern[str]:
    if fmt not in _DATE_RE_CACHE:
        out, pos = [], 0
        for m in _TOKEN_RE.finditer(fmt):
            out.append(re.escape(fmt[pos : m.start()]).replace(r"\ ", r"\s+"))
            out.append(_TOKEN_PATTERNS[m.group()])
            pos = m.end()
        out.append(re.escape(fmt[pos:]).replace(r"\ ", r"\s+"))
        _DATE_RE_CACHE[fmt] = re.compile("^" + "".join(out) + "$")
    return _DATE_RE_CACHE[fmt]


def parse_date(text: str, fmt: str = "DD.MM.YYYY", default_year: int | None = None) -> date:
    """Parse a printed date. ``fmt`` uses the tokens ``DD D MM M MMM YYYY YY``.

    Formats without a year (``D MMM``, ``MM/DD``) need ``default_year``.
    """
    m = _date_regex(fmt).match(text.strip())
    if not m:
        raise ValueError(f"date {text!r} does not match {fmt!r}")
    g = m.groupdict()
    if g.get("y4"):
        year = int(g["y4"])
    elif g.get("y2"):
        year = 2000 + int(g["y2"])
    elif default_year is not None:
        year = default_year
    else:
        raise ValueError(f"date {text!r} has no year and no default_year was given")
    month = int(g["m"]) if g.get("m") else month_from_name(g["mon"])
    return date(year, month, int(g["d"]))


def try_parse_date(text: str, fmt: str = "DD.MM.YYYY", default_year: int | None = None) -> date | None:
    try:
        return parse_date(text, fmt, default_year)
    except ValueError:
        return None


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def add_months(month: str, n: int) -> str:
    y, m = (int(p) for p in month.split("-"))
    idx = y * 12 + (m - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def months_between(a: str, b: str) -> int:
    """Number of months from ``a`` to ``b`` (``b - a``)."""
    ay, am = (int(p) for p in a.split("-"))
    by, bm = (int(p) for p in b.split("-"))
    return (by * 12 + bm) - (ay * 12 + am)


# ISO 4217 minor units. Every code not listed has two decimals.
_ZERO_DECIMAL = frozenset({
    "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "UYI", "VND", "VUV", "XAF", "XOF",
    "XPF",
})
_THREE_DECIMAL = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})
ISO_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def minor_units(currency: str) -> int:
    """Number of decimals of an ISO 4217 currency (``JPY`` 0, ``KWD`` 3, everything else 2)."""
    code = currency.upper()
    if not ISO_CURRENCY_RE.match(code):
        raise ValueError(f"not an ISO 4217 code: {currency!r}")
    return 0 if code in _ZERO_DECIMAL else 3 if code in _THREE_DECIMAL else 2


def decimal_to_minor(text: str, currency: str) -> int:
    """``"-12.30"`` (plain decimal, dot separator) to signed minor units of ``currency``. Exact, no floats."""
    from decimal import Decimal, InvalidOperation

    t = text.strip()
    if not re.match(r"^[+-]?\d+(?:\.\d+)?$", t):
        raise ValueError(f"not a plain decimal: {text!r}")
    try:
        value = Decimal(t) * (10 ** minor_units(currency))
    except InvalidOperation:
        raise ValueError(f"not a plain decimal: {text!r}") from None
    if value != value.to_integral_value():
        raise ValueError(f"{text!r} has more decimals than {currency} allows")
    return int(value)
