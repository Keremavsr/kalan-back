"""PDF text extraction runs on pdfium; PyMuPDF (AGPL) is only a dev tool for building fixtures."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from kalan.parsers.base import PAGE_BREAK
from kalan.pdf import PdfError, extract_text, page_count

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PDFS = sorted((ROOT / "tests" / "fixtures").rglob("*.pdf"))


def test_runtime_never_imports_pymupdf():
    pattern = re.compile(r"^\s*(?:import|from)\s+(?:pymupdf|fitz)\b|import_module\(\s*[\"'](?:pymupdf|fitz)", re.M)
    offenders = [p.relative_to(ROOT).as_posix() for p in (ROOT / "src").rglob("*.py")
                 if pattern.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
    code = ("import sys, kalan.api.app, kalan.cli, kalan.pipeline, kalan.template_check; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('pymupdf', 'fitz')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=ROOT).stdout.strip()
    assert out == "[]"


def test_fixtures_present():
    assert len(FIXTURE_PDFS) >= 44


@pytest.mark.parametrize("path", FIXTURE_PDFS, ids=lambda p: p.name)
def test_same_lines_as_the_reference_reader(path):
    """One line per printed cell, in drawing order, runs of spaces kept, the layout the parsers were built on."""
    with pymupdf.open(str(path)) as doc:
        reference = PAGE_BREAK.join(page.get_text() for page in doc)
    assert extract_text(path) == reference
    assert extract_text(path.read_bytes()) == reference


def test_page_count():
    path = next(p for p in FIXTURE_PDFS if p.parent.name == "yapikredi")
    with pymupdf.open(str(path)) as doc:
        assert page_count(path) == doc.page_count


def test_not_a_pdf():
    with pytest.raises(PdfError):
        extract_text(b"%PDF-1.4 broken")


def test_size_limit():
    with pytest.raises(ValueError, match="10 MB"):
        extract_text(b"%PDF" + b"0" * (10 * 1024 * 1024))
