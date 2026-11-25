"""PDF text extraction (pdfium). The only module that touches PDF bytes.

pdfium's own ``get_text_range`` joins every glyph on one baseline into a single line. The parsers were written
against the layout the statements come out with in common PDF readers: one line per printed cell (a label and
its value on two lines), in drawing order. :func:`extract_text` rebuilds that layout from the glyph positions:
glyphs are walked in content order and a new line starts whenever the next glyph is not a continuation of the
previous one (another baseline, a backward jump, or a forward gap wider than ``_SPLIT_GAP`` em).
"""

from __future__ import annotations

import ctypes
import math
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from kalan.parsers.base import PAGE_BREAK

MAX_PDF_BYTES = 10 * 1024 * 1024

# Distances in em (multiples of the font size), measured along / across the baseline.
_JOIN_GAP = 0.15  # closer than this: same word
_SPLIT_GAP = 0.8  # wider than this: a new cell, so a new line
_BASELINE_TOLERANCE = 0.8  # vertical move larger than this: a new line


class PdfError(ValueError):
    """The bytes are not a readable PDF."""


class PasswordError(PdfError):
    """The PDF is encrypted. ``wrong`` is True when a password was given but did not open it."""

    def __init__(self, wrong: bool) -> None:
        super().__init__("wrong PDF password" if wrong else "PDF needs a password")
        self.wrong = wrong


def _open(source: str | Path | bytes, password: str | None) -> pdfium.PdfDocument:
    if isinstance(source, bytes) and len(source) > MAX_PDF_BYTES:
        raise ValueError("PDF is larger than 10 MB")
    try:
        return pdfium.PdfDocument(source if isinstance(source, bytes) else str(source), password=password)
    except pdfium.PdfiumError as exc:
        if pdfium_c.FPDF_GetLastError() == pdfium_c.FPDF_ERR_PASSWORD:
            raise PasswordError(wrong=bool(password)) from exc
        raise PdfError("not a readable PDF") from exc


@dataclass(slots=True)
class _Glyph:
    char: str
    x: float  # origin on the baseline
    y: float
    end_x: float  # pen position after the glyph
    end_y: float
    dx: float  # unit vector of the writing direction
    dy: float
    size: float
    obj: int  # address of the text object the glyph belongs to


def _object_order(page: pdfium.PdfPage) -> dict[int, int]:
    """Text object address -> position in the page's content stream (form XObjects included)."""
    order: dict[int, int] = {}

    def walk(count: int, get) -> None:
        for n in range(count):
            obj = get(n)
            kind = pdfium_c.FPDFPageObj_GetType(obj)
            if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                order[ctypes.cast(obj, ctypes.c_void_p).value or 0] = len(order)
            elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                walk(pdfium_c.FPDFFormObj_CountObjects(obj), lambda k, f=obj: pdfium_c.FPDFFormObj_GetObject(f, k))

    walk(pdfium_c.FPDFPage_CountObjects(page.raw), lambda k: pdfium_c.FPDFPage_GetObject(page.raw, k))
    return order


def _glyphs(page: pdfium.PdfPage) -> list[_Glyph]:
    """Every printed glyph in content-stream order (pdfium's own order moves objects that share a line)."""
    order = _object_order(page)
    textpage = page.get_textpage()
    try:
        raw = textpage.raw
        x, y = ctypes.c_double(), ctypes.c_double()
        box = pdfium_c.FS_RECTF()
        found: list[tuple[int, int, _Glyph]] = []
        count = pdfium_c.FPDFText_CountChars(raw)
        i = 0
        while i < count:
            index = i
            code = pdfium_c.FPDFText_GetUnicode(raw, i)
            i += 1
            if pdfium_c.FPDFText_IsGenerated(raw, index) == 1 or code in (0, 0x0A, 0x0D, 0xFFFE, 0xFFFF):
                continue
            if code == 0x02:  # pdfium's mark for a hyphen at the end of a line
                char = "-"
            elif 0xD800 <= code < 0xDC00 and i < count:  # surrogate pair
                char = (chr(code) + chr(pdfium_c.FPDFText_GetUnicode(raw, i))).encode(
                    "utf-16", "surrogatepass").decode("utf-16", "replace")
                i += 1
            else:
                char = chr(code)
            pdfium_c.FPDFText_GetCharOrigin(raw, index, ctypes.byref(x), ctypes.byref(y))
            pdfium_c.FPDFText_GetLooseCharBox(raw, index, ctypes.byref(box))
            angle = pdfium_c.FPDFText_GetCharAngle(raw, index)
            dx, dy = (1.0, 0.0) if angle <= 0 else (math.cos(angle), -math.sin(angle))  # pdfium angles run clockwise
            advance = _advance(box, dx, dy)
            obj = ctypes.cast(pdfium_c.FPDFText_GetTextObject(raw, index), ctypes.c_void_p).value or 0
            glyph = _Glyph(char, x.value, y.value, x.value + advance * dx, y.value + advance * dy, dx, dy,
                           pdfium_c.FPDFText_GetFontSize(raw, index) or 1.0, obj)
            found.append((order.get(obj, len(order)), index, glyph))
    finally:
        textpage.close()
    found.sort(key=lambda item: (item[0], item[1]))
    return [glyph for _, _, glyph in found]


def _advance(box: pdfium_c.FS_RECTF, dx: float, dy: float) -> float:
    """Advance width of a glyph from its loose box, which pdfium gives axis-aligned.

    For rotated text the box is the bounding box of the rotated advance x height rectangle; undo that rotation.
    """
    width, height = box.right - box.left, box.top - box.bottom
    if abs(dy) < 1e-6:
        return width
    c, s = abs(dx), abs(dy)
    det = c * c - s * s
    if abs(det) < 0.2:  # close to 45 degrees: the box does not say; assume a square-ish glyph
        return max(width, height) / (c + s)
    return max(0.0, (width * c - height * s) / det)


def _page_lines(page: pdfium.PdfPage) -> list[str]:
    lines: list[str] = []
    current: list[str] = []
    prev: _Glyph | None = None
    space_width = 0.0  # advance of the last real space: pdfium keeps one glyph of a run of spaces
    for g in _glyphs(page):
        if prev is not None:
            gap_x, gap_y = g.x - prev.end_x, g.y - prev.end_y
            gap = gap_x * prev.dx + gap_y * prev.dy
            along = gap / g.size
            across = abs(gap_x * prev.dy - gap_y * prev.dx) / g.size
            turned = abs(g.dx - prev.dx) + abs(g.dy - prev.dy) > 0.05
            if (not turned and across < _BASELINE_TOLERANCE and along > _JOIN_GAP and prev.char == " "
                    and space_width > 0 and (g.obj == prev.obj or _near_whole(gap / space_width))):
                current.append(" " * round(gap / space_width))
            elif turned or across >= _BASELINE_TOLERANCE or abs(along) >= _SPLIT_GAP:
                lines.append("".join(current))
                current = []
            elif along > _JOIN_GAP and prev.char != " " and g.char != " ":
                current.append(" ")
        current.append(g.char)
        if g.char == " ":
            space_width = math.hypot(g.end_x - g.x, g.end_y - g.y)
        prev = g
    if current:
        lines.append("".join(current))
    return lines


def _near_whole(value: float) -> bool:
    return value >= 0.9 and abs(value - round(value)) < 0.1


def _text_of(doc: pdfium.PdfDocument) -> str:
    pages: list[str] = []
    for index in range(len(doc)):
        page = doc[index]
        try:
            pages.append("".join(line + "\n" for line in _page_lines(page)))
        finally:
            page.close()
    return PAGE_BREAK.join(pages)


def extract_text(source: str | Path | bytes, password: str | None = None) -> str:
    """Return the text of every page, one line per printed cell, pages joined with :data:`PAGE_BREAK`.

    Raises :class:`PasswordError` for an encrypted PDF without the right ``password`` and :class:`PdfError` for
    bytes that are not a PDF.
    """
    doc = _open(source, password)
    try:
        return _text_of(doc)
    finally:
        doc.close()


def page_count(source: str | Path | bytes, password: str | None = None) -> int:
    doc = _open(source, password)
    try:
        return len(doc)
    finally:
        doc.close()
