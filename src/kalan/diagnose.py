"""Anonymous diagnosis of a statement kalan could not read: structure only, safe to paste into an issue.

The report holds no text and no amounts from the statement. Every line becomes its *shape*: digits turn into
``9``, lower-case letters into ``a``, upper-case letters into ``A``; punctuation and single spaces stay, and a
run of three or more spaces becomes ``<N sp>``. Next to the shapes: page and line counts, which number and date
styles occur (counted by shape), and for every bank template which of its own anchor phrases (detection
phrases, header labels, table header, table end, summary title) were found. The anchors are kalan's template
text, not the user's.

``diagnose(path_or_bytes)`` is the entry point for the API route and for any command-line front end.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import kalan
from kalan.parsers import all_parsers
from kalan.parsers.template import TemplateParser
from kalan.pdf import PAGE_BREAK, extract_text, page_count
from kalan.text import fold

MAX_LINES = 600  # keeps the report short enough for an issue
_SPACE_RUN = re.compile(r" {3,}")

NUMBER_STYLES = {
    "1.234,56": re.compile(r"(?<![\d.,])\d{1,3}(?:\.\d{3})+,\d{2}(?![\d.,])|(?<![\d.,])\d{1,3},\d{2}(?![\d.,])"),
    "1,234.56": re.compile(r"(?<![\d.,])\d{1,3}(?:,\d{3})+\.\d{2}(?![\d.,])|(?<![\d.,])\d{1,3}\.\d{2}(?![\d.,])"),
    "1 234,56": re.compile(r"(?<![\d.,])\d{1,3}(?: \d{3})+,\d{2}(?![\d.,])"),
}
DATE_STYLES = {
    "DD.MM.YYYY": re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b"),
    "DD/MM/YYYY": re.compile(r"\b\d{2}/\d{2}/\d{4}\b"),
    "YYYY-MM-DD": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "DD.MM.YY": re.compile(r"\b\d{2}\.\d{2}\.\d{2}\b(?!\.)"),
    "D Month YYYY": re.compile(r"\b\d{1,2} [^\W\d_]{3,} \d{4}\b"),
}


def shape(line: str) -> str:
    """``Ödeme 1.234,56 TL`` -> ``Aaaaa 9.999,99 AA``. No letter or digit survives."""
    out = []
    for ch in line:
        if ch.isdigit():
            out.append("9")
        elif ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        elif ch.isspace():
            out.append(" ")
        else:
            out.append(ch)
    return _SPACE_RUN.sub(lambda m: f"<{len(m.group())} sp>", "".join(out)).rstrip(" ")


def _template_anchors(parser: TemplateParser, folded_text: str, folded_lines: list[str]) -> dict[str, Any]:
    t = parser.template

    def found(label: str, *, whole_line: bool = False, line_start: bool = False) -> bool:
        f = fold(label)
        if whole_line:
            return f in folded_lines
        if line_start:
            return any(ln.startswith(f) for ln in folded_lines)
        return f in folded_text

    def tally(labels: list[str], **how: bool) -> dict[str, Any]:
        hits = [label for label in labels if found(label, **how)]
        return {"found": len(hits), "total": len(labels), "missing": [label for label in labels if label not in hits]}

    header = {name: any(found(label, line_start=True) for label in labels) for name, labels in t.header.fields.items()}
    return {
        "detect_all": tally(t.detect.all),
        "detect_none_present": [p for p in t.detect.none if found(p)],
        "header_fields": header,
        "table_header": tally(t.table.header, whole_line=True),
        "table_end": any(found(label, line_start=True) for label in t.table.end),
        "summary_title": None if t.summary is None else found(t.summary.title),
    }


def diagnose(source: str | Path | bytes, password: str | None = None) -> dict[str, Any]:
    """Structure-only report of a statement PDF (or of extracted text given as a ``.txt`` path).

    Raises :class:`kalan.pdf.PasswordError` for an encrypted PDF without the right password and
    :class:`kalan.pdf.PdfError` for a file that is not a PDF.
    """
    if isinstance(source, str | Path) and Path(source).suffix.lower() == ".txt":
        text, pages = Path(source).read_text(encoding="utf-8"), None
    else:
        text = extract_text(source, password=password)
        pages = page_count(source, password=password)
    page_texts = text.split(PAGE_BREAK)
    raw_lines = [ln for page in page_texts for ln in page.split("\n")]
    lines = [ln for ln in raw_lines if ln.strip()]
    folded_lines = [fold(ln.strip()) for ln in lines]
    folded_text = fold(text)

    shapes: list[dict[str, Any]] = []
    for p, page in enumerate(page_texts, start=1):
        for ln in page.split("\n"):
            s = shape(ln)
            if not s:
                continue
            if shapes and shapes[-1]["page"] == p and shapes[-1]["shape"] == s:
                shapes[-1]["repeat"] += 1
            else:
                shapes.append({"page": p, "shape": s, "repeat": 1})

    templates = []
    for parser in sorted(all_parsers(), key=lambda q: q.id):
        entry: dict[str, Any] = {"id": parser.id, "detected": parser.detect(text)}
        if isinstance(parser, TemplateParser):
            entry["anchors"] = _template_anchors(parser, folded_text, folded_lines)
        templates.append(entry)

    return {
        "kalan_version": kalan.__version__,
        "page_count": pages if pages is not None else len(page_texts),
        "has_text": any(ch.isalnum() for ch in text),
        "line_count": len(lines),
        "lines_per_page": [sum(1 for ln in page.split("\n") if ln.strip()) for page in page_texts],
        "number_styles": {k: sum(len(r.findall(ln)) for ln in lines) for k, r in NUMBER_STYLES.items()},
        "date_styles": {k: sum(len(r.findall(ln)) for ln in lines) for k, r in DATE_STYLES.items()},
        "templates": templates,
        "shapes": shapes[:MAX_LINES],
        "shapes_truncated": len(shapes) > MAX_LINES,
    }


def as_text(report: dict[str, Any]) -> str:
    """The report as plain text for an issue form: summary lines, then one shape per line."""
    out = [
        f"kalan {report['kalan_version']}",
        f"pages: {report['page_count']}  lines: {report['line_count']}  per page: {report['lines_per_page']}",
        f"text layer: {'yes' if report['has_text'] else 'no (scanned image?)'}",
        "numbers: " + ", ".join(f"{k} x{v}" for k, v in report["number_styles"].items() if v),
        "dates: " + ", ".join(f"{k} x{v}" for k, v in report["date_styles"].items() if v),
    ]
    for t in report["templates"]:
        line = f"template {t['id']}: detected={'yes' if t['detected'] else 'no'}"
        a = t.get("anchors")
        if a:
            line += (f"  detect {a['detect_all']['found']}/{a['detect_all']['total']}"
                     f"  table header {a['table_header']['found']}/{a['table_header']['total']}"
                     f"  header fields {sum(a['header_fields'].values())}/{len(a['header_fields'])}"
                     f"  table end {'yes' if a['table_end'] else 'no'}")
        out.append(line)
    out.append("--- shapes (page | shape | repeat)")
    out.extend(f"{s['page']} | {s['shape']}" + (f" | x{s['repeat']}" if s["repeat"] > 1 else "")
               for s in report["shapes"])
    if report["shapes_truncated"]:
        out.append("... (truncated)")
    return "\n".join(out) + "\n"
