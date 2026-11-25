"""PDF text extraction (pdfium). The only module that touches PDF bytes.

pdfium's own ``get_text_range`` joins every glyph on one baseline into a single line. The parsers were written
against the layout the statements come out with in common PDF readers: one line per printed cell (a label and
its value on two lines), in drawing order. :func:`extract_text` rebuilds that layout from the glyph positions:
glyphs are walked in content order and a new line starts whenever the next glyph is not a continuation of the
previous one (another baseline, a backward jump, or a forward gap wider than ``_SPLIT_GAP`` em). pdfium keeps
only the first space of a run of spaces; the run's real length is restored from the glyph positions and the
text object's bounds, so a cell padded with spaces still ends where its padding ends and the spaces never
bridge the gap to the next cell. Glyphs outside the page are dropped.
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
    end_x: float  # pen position after the glyph (after the whole run, for a space that stands for a run)
    end_y: float
    width: float  # advance of this one glyph
    dx: float  # unit vector of the writing direction
    dy: float
    size: float
    obj: int  # address of the text object the glyph belongs to


_Bounds = tuple[float, float, float, float]  # left, bottom, right, top in page space


def _text_objects(page: pdfium.PdfPage) -> tuple[dict[int, int], dict[int, _Bounds]]:
    """Text object address -> position in the content stream (form XObjects included), and -> bounds.

    Bounds are kept for objects directly on the page only (inside a form XObject they are in form space).
    """
    order: dict[int, int] = {}
    bounds: dict[int, _Bounds] = {}
    edges = [ctypes.c_float() for _ in range(4)]

    def walk(count: int, get, top: bool) -> None:
        for n in range(count):
            obj = get(n)
            kind = pdfium_c.FPDFPageObj_GetType(obj)
            if kind == pdfium_c.FPDF_PAGEOBJ_TEXT:
                address = ctypes.cast(obj, ctypes.c_void_p).value or 0
                order[address] = len(order)
                if top and pdfium_c.FPDFPageObj_GetBounds(obj, *(ctypes.byref(e) for e in edges)):
                    left, bottom, right, top_edge = (e.value for e in edges)
                    bounds[address] = (left, bottom, right, top_edge)
            elif kind == pdfium_c.FPDF_PAGEOBJ_FORM:
                walk(pdfium_c.FPDFFormObj_CountObjects(obj), lambda k, f=obj: pdfium_c.FPDFFormObj_GetObject(f, k),
                     False)

    walk(pdfium_c.FPDFPage_CountObjects(page.raw), lambda k: pdfium_c.FPDFPage_GetObject(page.raw, k), True)
    return order, bounds


def _glyphs(page: pdfium.PdfPage) -> list[_Glyph]:
    """Every printed glyph in content-stream order (pdfium's own order moves objects that share a line).

    pdfium keeps only the first space of a run of spaces. Inside a text object the next glyph's position
    shows how long the run was; a run that ends the object is measured to the object's right edge, so
    trailing padding counts as width (a padded cell then ends where its padding ends, as printed).
    """
    order, bounds = _text_objects(page)
    textpage = page.get_textpage()
    try:
        raw = textpage.raw
        x, y = ctypes.c_double(), ctypes.c_double()
        box = pdfium_c.FS_RECTF()
        found: list[tuple[int, int, _Glyph]] = []
        media_left, media_bottom, media_right, media_top = page.get_mediabox()
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
            if (box.right < media_left or box.left > media_right or box.top < media_bottom
                    or box.bottom > media_top):
                continue  # printed outside the page: not visible, not text
            angle = pdfium_c.FPDFText_GetCharAngle(raw, index)
            dx, dy = (1.0, 0.0) if angle <= 0 else (math.cos(angle), -math.sin(angle))  # pdfium angles run clockwise
            advance = _advance(box, dx, dy)
            obj = ctypes.cast(pdfium_c.FPDFText_GetTextObject(raw, index), ctypes.c_void_p).value or 0
            glyph = _Glyph(char, x.value, y.value, x.value + advance * dx, y.value + advance * dy, advance, dx, dy,
                           pdfium_c.FPDFText_GetFontSize(raw, index) or 1.0, obj)
            found.append((order.get(obj, len(order)), index, glyph))
    finally:
        textpage.close()
    found.sort(key=lambda item: (item[0], item[1]))
    glyphs = [glyph for _, _, glyph in found]
    for k, g in enumerate(glyphs):
        last_of_object = k + 1 == len(glyphs) or glyphs[k + 1].obj != g.obj
        if g.char == " " and last_of_object and g.obj in bounds:
            left, bottom, right, top = bounds[g.obj]
            # A space has no ink: the object's edge is the origin of the run's last space, one advance short.
            reach = max((cx - g.x) * g.dx + (cy - g.y) * g.dy for cx in (left, right) for cy in (bottom, top))
            reach += g.width
            if reach > g.width * 1.5:
                g.end_x, g.end_y = g.x + reach * g.dx, g.y + reach * g.dy
    return glyphs


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
    for g in _glyphs(page):
        if prev is not None:
            gap_x, gap_y = g.x - prev.end_x, g.y - prev.end_y
            gap = gap_x * prev.dx + gap_y * prev.dy
            along = gap / g.size
            across = abs(gap_x * prev.dy - gap_y * prev.dx) / g.size
            turned = abs(g.dx - prev.dx) + abs(g.dy - prev.dy) > 0.05
            if (not turned and across < _BASELINE_TOLERANCE and along > _JOIN_GAP and prev.char == " "
                    and prev.width > 0 and g.obj == prev.obj):
                current.append(" " * round(gap / prev.width))  # the rest of a run of spaces inside one cell
            elif turned or across >= _BASELINE_TOLERANCE or abs(along) >= _SPLIT_GAP:
                lines.append("".join(current))
                current = []
            elif along > _JOIN_GAP and prev.char != " " and g.char != " ":
                current.append(" ")
        if g.char == " " and g.width > 0:
            current.append(" " * max(1, round(math.hypot(g.end_x - g.x, g.end_y - g.y) / g.width)))
        else:
            current.append(g.char)
        prev = g
    if current:
        lines.append("".join(current))
    return lines


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
