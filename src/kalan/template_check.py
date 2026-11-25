"""``kalan template check <template.yaml> <statement.pdf|.txt> [--quiet]``.

Validates a template, runs it on one statement (redacted first, as in the app) and prints the result
field by field, then the reconciliation. ``--quiet`` prints only found/missing, counts and statuses,
no values, so the output can be pasted into a public issue. Descriptions are never printed.

Exit code: 0 parsed and reconciled, 1 parsed with a mismatch or not detected, 2 invalid template or
broken layout.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from pydantic import ValidationError

from kalan.parsers.base import ParseError
from kalan.parsers.template import REQUIRED_HEADER, TemplateParser, load_template
from kalan.pdf import extract_text
from kalan.reconcile import reconcile
from kalan.redact import redact
from kalan.text import fold


def _validation_lines(exc: ValidationError) -> list[str]:
    return [f"  {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()]


def check(template_path: str | Path, statement_path: str | Path, quiet: bool = False) -> tuple[list[str], int]:
    out: list[str] = []
    try:
        template = load_template(template_path)
    except ValidationError as exc:
        return ["template: invalid", *_validation_lines(exc)], 2
    except (OSError, ValueError) as exc:
        return [f"template: invalid ({exc})"], 2
    parser = TemplateParser(template, source=Path(template_path))
    out.append(f"template: {template.id} valid")

    src = Path(statement_path)
    try:
        raw = src.read_text(encoding="utf-8") if src.suffix.lower() == ".txt" else extract_text(src)
    except Exception as exc:  # report the type only
        return [*out, f"statement: cannot read ({type(exc).__name__})"], 2
    text = redact(raw).text
    detected = parser.detect(text)
    out.append(f"detect: {'yes' if detected else 'no'}")

    lines = [ln.strip() for ln in text.replace("\r", "").replace("\f", "\n").split("\n")]
    lines = [ln for ln in lines if ln]
    header = parser.header_fields(lines, [fold(ln) for ln in lines])
    out.append("header:")
    for name in template.header.fields:
        need = "required" if name in REQUIRED_HEADER else "optional"
        if name in header:
            value = "" if quiet or name == "card" else f"  {header[name]}"
            out.append(f"  {name}: found{value}")
        else:
            out.append(f"  {name}: missing ({need})")

    try:
        st = parser.parse(text)
    except ParseError as exc:
        reason = str(exc).split(":")[0] if quiet else str(exc)
        return [*out, f"parse: failed ({reason})"], 2
    kinds = Counter(t.kind for t in st.transactions)
    plans = {t.installment.plan_id for t in st.transactions if t.installment is not None}
    out.append(f"rows: {len(st.transactions)} ({', '.join(f'{k} {n}' for k, n in sorted(kinds.items())) or 'none'})")
    out.append(f"installment rows: {sum(t.installment is not None for t in st.transactions)}, plans: {len(plans)}")
    out.append(f"foreign currency rows: {sum(t.original_currency is not None for t in st.transactions)}")
    out.append(f"interest rates: {len(st.interest_rates)}")
    out.append(f"summary: {'found' if st.printed_summary else 'missing'}")
    r = reconcile(st)
    out.append(f"reconciliation: {r.status}")
    for c in r.checks:
        detail = "" if quiet else f"  expected {c.expected} computed {c.computed} difference {c.difference}"
        out.append(f"  {c.name}: {c.status}{detail}")
    return out, 0 if detected and r.status == "ok" else 1
