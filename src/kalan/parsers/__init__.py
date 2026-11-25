"""Parser registry.

Every module in this package named ``<country>_<bank>_<product>.py`` (lower case, for example
``tr_vakifbank_worldcard.py``) is imported automatically and every :class:`StatementParser`
subclass defined in it is registered. Declarative templates are registered too: the built-in ones in
``src/kalan/templates/*.yaml`` and the user's own in ``<data dir>/templates/*.yaml`` (read at start;
:func:`reload` reads them again). See ``docs/adding-a-bank.md``.

Precedence: a Python parser, then a built-in template, then a user template. A user template whose id
is already taken, or that does not validate, is skipped and listed in :func:`template_errors`.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import re
from functools import cache
from pathlib import Path

from kalan.config import data_dir
from kalan.parsers.base import PAGE_BREAK, ParseError, StatementParser
from kalan.parsers.template import BUILTIN_DIR, TemplateParser, load_template

__all__ = [
    "PAGE_BREAK",
    "ParseError",
    "StatementParser",
    "all_parsers",
    "detect_parser",
    "get_parser",
    "reload",
    "template_errors",
    "user_templates_dir",
]

log = logging.getLogger(__name__)

MODULE_NAME_RE = re.compile(r"^[a-z]{2}_[a-z0-9]+_[a-z0-9]+$")


class UnknownFormatError(LookupError):
    """No registered parser recognised the statement."""


def user_templates_dir() -> Path:
    return data_dir() / "templates"


@cache
def _registry(user_dir: str) -> tuple[dict[str, StatementParser], tuple[str, ...]]:
    found: dict[str, StatementParser] = {}
    claimed: set[str] = set()
    errors: list[str] = []
    for info in pkgutil.iter_modules(__path__):
        if not MODULE_NAME_RE.match(info.name):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, StatementParser) and not inspect.isabstract(obj) and obj.__module__ == module.__name__:
                parser = obj()
                if parser.id != info.name:
                    raise RuntimeError(f"parser id {parser.id!r} must equal its module name {info.name!r}")
                if parser.id in found:
                    raise RuntimeError(f"duplicate parser id {parser.id!r}")
                found[parser.id] = parser
                if issubclass(obj, TemplateParser) and obj.template_file:
                    claimed.add(obj.template_file)

    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        if path.name in claimed:
            continue
        parser = TemplateParser(load_template(path), source=path)
        if parser.id in found:
            raise RuntimeError(f"built-in template {path.name} reuses parser id {parser.id!r}")
        found[parser.id] = parser

    udir = Path(user_dir)
    for path in sorted(udir.glob("*.yaml")) if udir.is_dir() else []:
        try:
            parser = TemplateParser(load_template(path), source=path)
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        if parser.id in found:
            errors.append(f"{path.name}: id {parser.id!r} is already used by a built-in parser")
            continue
        found[parser.id] = parser
    for e in errors:
        log.warning("user template skipped: %s", e.splitlines()[0])
    return found, tuple(errors)


def _parsers() -> dict[str, StatementParser]:
    return _registry(str(user_templates_dir()))[0]


def template_errors() -> list[str]:
    """User templates that were skipped, with the reason."""
    return list(_registry(str(user_templates_dir()))[1])


def reload() -> None:
    """Read the template folders again (after a user adds or edits a template)."""
    _registry.cache_clear()


def all_parsers() -> list[StatementParser]:
    return [p for _, p in sorted(_parsers().items())]


def get_parser(parser_id: str) -> StatementParser:
    try:
        return _parsers()[parser_id]
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
