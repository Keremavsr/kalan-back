"""PDF text extraction (PyMuPDF). The only module that touches PDF bytes."""

from __future__ import annotations

from pathlib import Path

import pymupdf

from kalan.parsers.base import PAGE_BREAK

MAX_PDF_BYTES = 10 * 1024 * 1024


def extract_text(source: str | Path | bytes) -> str:
    """Return ``page.get_text()`` of every page joined with :data:`PAGE_BREAK`."""
    if isinstance(source, bytes):
        if len(source) > MAX_PDF_BYTES:
            raise ValueError("PDF is larger than 10 MB")
        doc = pymupdf.open(stream=source, filetype="pdf")
    else:
        doc = pymupdf.open(str(source))
    with doc:
        return PAGE_BREAK.join(page.get_text() for page in doc)
