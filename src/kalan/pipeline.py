"""PDF or text in, redacted and parsed :class:`Statement` out.

Redaction always runs first; parsers only ever see redacted text.
"""

from __future__ import annotations

from pathlib import Path

from kalan.models import Statement
from kalan.parsers import detect_parser
from kalan.pdf import extract_text
from kalan.reconcile import reconcile
from kalan.redact import redact


def parse_text(text: str) -> Statement:
    result = redact(text)
    parser = detect_parser(result.text)
    statement = parser.parse(result.text)
    if statement.card_last4 is None and result.card_last4 is not None:
        statement = statement.model_copy(update={"card_last4": result.card_last4})
    return statement.model_copy(update={"reconciliation": reconcile(statement)})


def parse_pdf(source: str | Path | bytes, password: str | None = None) -> Statement:
    """Parse a statement PDF. An encrypted one needs ``password`` (:class:`kalan.pdf.PasswordError` otherwise)."""
    return parse_text(extract_text(source, password=password))
