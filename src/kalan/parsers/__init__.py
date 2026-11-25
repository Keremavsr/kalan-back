"""Parser registry.

Every module in this package named ``<country>_<bank>_<product>.py`` (lower case, for example
``tr_vakifbank_worldcard.py``) is imported automatically and every :class:`StatementParser`
subclass defined in it is registered. See ``docs/adding-a-bank.md``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from functools import cache

from kalan.parsers.base import PAGE_BREAK, ParseError, StatementParser

__all__ = ["PAGE_BREAK", "ParseError", "StatementParser", "all_parsers", "detect_parser", "get_parser"]

MODULE_NAME_RE = re.compile(r"^[a-z]{2}_[a-z0-9]+_[a-z0-9]+$")


class UnknownFormatError(LookupError):
    """No registered parser recognised the statement."""


@cache
def _registry() -> dict[str, StatementParser]:
    found: dict[str, StatementParser] = {}
    for info in pkgutil.iter_modules(__path__):
        if not MODULE_NAME_RE.match(info.name):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, StatementParser) and not inspect.isabstract(obj) and obj.__module__ == module.__name__:
                if obj.id != info.name:
                    raise RuntimeError(f"parser id {obj.id!r} must equal its module name {info.name!r}")
                if obj.id in found:
                    raise RuntimeError(f"duplicate parser id {obj.id!r}")
                found[obj.id] = obj()
    return found


def all_parsers() -> list[StatementParser]:
    return [p for _, p in sorted(_registry().items())]


def get_parser(parser_id: str) -> StatementParser:
    try:
        return _registry()[parser_id]
    except KeyError:
        raise UnknownFormatError(parser_id) from None


def detect_parser(text: str) -> StatementParser:
    """Return the single parser that recognises ``text``."""
    matches = [p for p in all_parsers() if p.detect(text)]
    if not matches:
        raise UnknownFormatError("no parser recognised this statement")
    if len(matches) > 1:
        raise UnknownFormatError("several parsers claim this statement: " + ", ".join(p.id for p in matches))
    return matches[0]
