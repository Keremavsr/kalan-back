"""Data commands of the ``kalan`` CLI (everything except ``serve``, ``mcp`` and ``template``).

A command is one function in a module of this package, registered with :func:`command`. It receives
the :class:`~kalan.service.Kalan` facade and the parsed arguments and returns an :class:`Out`: the data
printed by ``--json`` and the lines printed for a person. Modules here are imported automatically, so a
new command group is a new file and nothing else changes::

    @command("borc", "debt", help="what is left to pay", args=[arg("--card")])
    def debt(k: Kalan, a: argparse.Namespace) -> Out:
        data = ...
        return Out(data, table(["Card", "Left"], rows))

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
from kalan.service import Kalan

COMMA_LOCALES = {"tr", "pt", "es"}


@dataclass
class Out:
    data: Any
    lines: list[str] = field(default_factory=list)


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
    """Formats minor units for one locale: ``1.234,56 TRY`` (tr) or ``1,234.56 TRY`` (en)."""

    def __init__(self, locale: str) -> None:
        self.base: AmountFormat = COMMA_DECIMAL if locale in COMMA_LOCALES else DOT_DECIMAL

    def __call__(self, minor: int | None, currency: str | None = None) -> str:
        if minor is None:
            return "-"
        decimals = minor_units(currency) if currency else 2
        fmt = AmountFormat(self.base.thousands, self.base.decimal, decimals)
        text = format_minor(minor, fmt)
        return f"{text} {currency}" if currency else text


def money_for(k: Kalan) -> Money:
    return Money(k.store.get_settings().locale)


def percent(share: str | None) -> str:
    """``"0.25"`` -> ``"25.0%"``; ``"-0.10"`` -> ``"-10.0%"``."""
    if share is None:
        return "-"
    return f"{float(share) * 100:.1f}%"


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
