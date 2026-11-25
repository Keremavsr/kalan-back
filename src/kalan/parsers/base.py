"""Base class for statement parsers.

A parser turns the plain text of one statement (:func:`kalan.pdf.extract_text`: every page, joined with
:data:`PAGE_BREAK`) into a :class:`~kalan.models.Statement`. Parsers are pure: no file, network or
database access, so the same code runs in tests, in the API and in tools.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from typing import ClassVar

from kalan.amounts import AmountFormat
from kalan.models import Statement

PAGE_BREAK = "\f"
"""Separator between pages in the text handed to a parser."""


class ParseError(ValueError):
    """The text looked like this parser's format but a required part is missing or malformed."""


class StatementParser(ABC):
    """One bank product (for example VakıfBank Worldcard).

    Subclasses set the class attributes and implement :meth:`detect` and :meth:`parse`.
    """

    id: ClassVar[str]
    """Same as the module name: ``<country>_<bank>_<product>``."""
    bank: ClassVar[str]
    product: ClassVar[str]
    country: ClassVar[str]
    currency: ClassVar[str]
    amount_format: ClassVar[AmountFormat]
    date_format: ClassVar[str] = "DD.MM.YYYY"
    password_hint: ClassVar[str | None] = None
    """What the bank uses as the PDF password, e.g. "last 6 digits of your national ID". Never a password."""

    @abstractmethod
    def detect(self, text: str) -> bool:
        """Return True if ``text`` is a statement of this bank product. Must be cheap and strict."""

    @abstractmethod
    def parse(self, text: str) -> Statement:
        """Parse redacted statement text. Raise :class:`ParseError` on a broken layout."""

    @staticmethod
    def pages(text: str) -> list[str]:
        return text.split(PAGE_BREAK)

    @staticmethod
    def stable_id(prefix: str, *parts: object) -> str:
        """Deterministic id from ``parts`` so re-parsing the same statement gives the same ids."""
        digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]
        return f"{prefix}_{digest}"
