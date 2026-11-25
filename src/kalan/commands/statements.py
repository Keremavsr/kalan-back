"""``kalan import``, ``kalan statements``, ``kalan banks``."""

from __future__ import annotations

import argparse

from kalan.commands import Out, arg, command, money_for, shorten, table
from kalan.service import Kalan


@command("import", "yukle", order=10, help="import a statement PDF or a CSV, OFX or QIF file", args=[
    arg("file", help="statement PDF (or its extracted .txt), or a .csv, .ofx or .qif export"),
    arg("--bank", help="parser or template id (see `kalan banks`); for a CSV, the template whose rules apply"),
    arg("--no-ai", dest="ai_fallback", action="store_false",
        help="do not send an unrecognised statement to the selected AI provider"),
])
def import_file(k: Kalan, a: argparse.Namespace) -> Out:
    result = k.import_file(a.file, bank=a.bank, ai_fallback=a.ai_fallback)
    money = money_for(k)
    if result["kind"] == "statement":
        st = result["statement"]
        rec = (st.get("reconciliation") or {}).get("status", "none")
        lines = [
            f"Stored statement {st['id']}: {st['bank']} {st['product']}"
            + (f" *{st['card_last4']}" if st.get("card_last4") else ""),
            f"  period {st['period_start']} .. {st['period_end']}, due {st['due_date']}",
            f"  total {money(st['statement_total'], st['currency'])}, "
            f"minimum {money(st['minimum_payment'], st['currency'])}",
            f"  {len(st['transactions'])} transactions, totals check: {rec}"
            + (", needs review" if st.get("needs_review") else ""),
        ]
        return Out(result, lines)
    lines = [
        f"Imported {result['imported']} of {result['rows']} rows ({result['format']}, {result['currency']})"
        f" as {result['id']}",
    ]
    if result["skipped_duplicates"]:
        lines.append(f"  {result['skipped_duplicates']} rows skipped as duplicates")
    if result["months"]:
        lines.append(f"  months: {', '.join(result['months'])}")
    return Out(result, lines)


@command("statements", "ekstreler", order=20, help="list stored statements, newest first")
def statements(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.statements()
    money = money_for(k)
    rows = [
        [s.id, shorten(f"{s.bank} {s.product}", 28), s.card_last4 or "", s.period_end.isoformat(),
         s.due_date.isoformat(), money(s.statement_total, s.currency), money(s.minimum_payment, s.currency),
         s.reconciliation.status if s.reconciliation else "none", s.analysis_status]
        for s in items
    ]
    lines = table(["Id", "Card", "Last4", "Cut-off", "Due", "Total", "Minimum", "Check", "AI"], rows, right=[5, 6])
    return Out({"items": [s.model_dump(mode="json") for s in items]}, lines or ["No statements yet."])


@command("banks", "bankalar", order=90, help="parser and template ids for `import --bank`")
def banks(k: Kalan, a: argparse.Namespace) -> Out:
    ids = k.banks()
    return Out({"items": ids}, ids)
