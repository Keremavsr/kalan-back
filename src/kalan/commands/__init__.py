"""Data commands of the ``kalan`` CLI (everything except ``serve``, ``mcp`` and ``template``).

A command is one function in a module of this package, registered with :func:`command`. It receives
the :class:`~kalan.service.Kalan` facade and the parsed arguments and returns an :class:`Out`: the data
printed by ``--json`` and the lines printed for a person. Modules here are imported automatically, so a
new command group is a new file and nothing else changes::

    @command("borc", "debt", help="help.borc", args=[arg("--card", help="help.borc.card")])
    def debt(k: Kalan, a: argparse.Namespace) -> Out:
        data = k.debt(a.card)                      # one Kalan method in service.py
        return Out(data, table(cols("card", "left"), rows))

Every text a person reads comes from :mod:`kalan.i18n` (``t("key")``, ``cols(...)``, ``word(...)``); help
texts are catalogue keys. Add the English and Turkish strings there, nowhere else.

Money is always integer minor units in ``--json`` and formatted with the user's locale in tables.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from kalan.amounts import COMMA_DECIMAL, DOT_DECIMAL, AmountFormat, format_minor, minor_units
from kalan.i18n import get_lang, has, t
from kalan.service import Kalan

COMMA_LOCALES = {"tr", "pt", "es"}


@dataclass
class Out:
    """``data`` for ``--json``; ``lines`` for a person, or ``raw`` text written as is (an export file);
    ``notes`` go to stderr so they never mix with ``raw`` output."""

    data: Any
    lines: list[str] = field(default_factory=list)
    raw: str | None = None
    notes: list[str] = field(default_factory=list)


Handler = Callable[[Kalan, argparse.Namespace], Out]


@dataclass
class Command:
    name: str
    aliases: tuple[str, ...]
    help: str
    args: Sequence[tuple[tuple[str, ...], dict[str, Any]]]
    handler: Handler
    order: int = 100


COMMANDS: dict[str, Command] = {}


def arg(*names: str, **kwargs: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Arguments for :func:`command`, exactly as for ``ArgumentParser.add_argument``."""
    return names, kwargs


def command(name: str, *aliases: str, help: str, args: Sequence[tuple[tuple[str, ...], dict[str, Any]]] = (),
            order: int = 100) -> Callable[[Handler], Handler]:
    """Register a command; ``order`` sorts it in ``kalan --help`` (lower first)."""

    def register(fn: Handler) -> Handler:
        if name in COMMANDS:
            raise RuntimeError(f"duplicate command {name!r}")
        COMMANDS[name] = Command(name, aliases, help, args, fn, order)
        return fn

    return register


def load() -> dict[str, Command]:
    """Import every module of this package (each registers its commands) and return the registry."""
    for info in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{info.name}")
    return dict(sorted(COMMANDS.items(), key=lambda kv: (kv[1].order, kv[0])))


# -- formatting -----------------------------------------------------------------------------------


class Money:
    """Formats minor units for one output language: ``1.234,56 TL`` (tr) or ``1,234.56 TRY`` (en)."""

    def __init__(self, lang: str) -> None:
        self.lang = lang
        self.base: AmountFormat = COMMA_DECIMAL if lang in COMMA_LOCALES else DOT_DECIMAL

    def unit(self, currency: str) -> str:
        return "TL" if self.lang == "tr" and currency == "TRY" else currency

    def __call__(self, minor: int | None, currency: str | None = None) -> str:
        if minor is None:
            return "-"
        decimals = minor_units(currency) if currency else 2
        fmt = AmountFormat(self.base.thousands, self.base.decimal, decimals)
        text = format_minor(minor, fmt)
        return f"{text} {self.unit(currency)}" if currency else text


def money_for(k: Kalan | None = None) -> Money:
    """Money formatter for the current output language (see :mod:`kalan.i18n`)."""
    return Money(get_lang())


def _number(value: float, decimals: int) -> str:
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if get_lang() in COMMA_LOCALES else text


def percent(share: str | float | None, decimals: int = 1) -> str:
    """A fraction as a percentage: ``"0.25"`` -> ``"25.0%"`` (en) or ``"%25,0"`` (tr)."""
    if share is None:
        return "-"
    text = _number(float(share) * 100, decimals)
    if get_lang() == "tr":
        return f"-%{text[1:]}" if text.startswith("-") else f"%{text}"
    return f"{text}%"


def rate(percent_text: str | None) -> str:
    """A rate already in percent (``"3.08"``) -> ``"3.08%"`` (en) or ``"%3,08"`` (tr)."""
    if percent_text is None:
        return "-"
    return percent(float(percent_text) / 100, decimals=2)


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 0 if unicodedata.combining(ch) else 1
               for ch in text)


def table(headers: Sequence[str], rows: Iterable[Sequence[Any]], right: Iterable[int] = ()) -> list[str]:
    """Plain text table; columns listed in ``right`` are right-aligned (use it for amounts)."""
    body = [["" if c is None else str(c) for c in row] for row in rows]
    if not body:
        return []
    widths = [max(_width(r[i]) for r in [list(headers), *body]) for i in range(len(headers))]
    right_cols = set(right)

    def line(cells: Sequence[str]) -> str:
        out = []
        for i, cell in enumerate(cells):
            pad = " " * (widths[i] - _width(cell))
            out.append(pad + cell if i in right_cols else cell + pad)
        return "  ".join(out).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(r) for r in body)]


def shorten(text: str | None, limit: int = 40) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def helptext(value: str | None) -> str | None:
    """A help text given as a catalogue key (``"help.takvim"``) in the current language; other text as is."""
    return t(value) if value and has(value) else value


def cols(*keys: str) -> list[str]:
    """Column headers from the catalogue: ``cols("month", "total")``."""
    return [t(f"col.{k}") for k in keys]
