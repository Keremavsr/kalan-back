"""``kalan import``, ``kalan statements``, ``kalan banks``, ``kalan teshis``."""

from __future__ import annotations

import argparse
import getpass

from kalan.commands import Out, arg, cols, command, money_for, shorten, table
from kalan.i18n import t, word
from kalan.service import Kalan

ASK = object()  # ``--password`` given without a value


PASSWORD_ARG = arg("--password", "--sifre", nargs="?", const=ASK, metavar="PASSWORD", help="help.password")


def password(a: argparse.Namespace) -> str | None:
    """``--password VALUE``, or a hidden prompt for ``--password`` alone (keeps it out of the shell history)."""
    if a.password is ASK:
        return getpass.getpass(t("password.prompt")) or None
    return a.password or None


@command("import", "yukle", order=10, help="help.import", args=[
    arg("file", help="help.import.file"),
    arg("--bank", help="help.import.bank"),
    arg("--no-ai", dest="ai_fallback", action="store_false", help="help.import.no_ai"),
    PASSWORD_ARG,
])
def import_file(k: Kalan, a: argparse.Namespace) -> Out:
    result = k.import_file(a.file, bank=a.bank, ai_fallback=a.ai_fallback, password=password(a))
    money = money_for(k)
    if result["kind"] == "statement":
        st = result["statement"]
        cur = st["currency"]
        rec = (st.get("reconciliation") or {}).get("status", "none")
        card = f"{st['bank']} {st['product']}" + (f" *{st['card_last4']}" if st.get("card_last4") else "")
        lines = [
            t("import.stored", id=st["id"], card=card),
            t("import.period", start=st["period_start"], end=st["period_end"], due=st["due_date"]),
            t("import.totals", total=money(st["statement_total"], cur), minimum=money(st["minimum_payment"], cur)),
            t("import.rows", count=len(st["transactions"]), status=word("status", rec))
            + (t("import.review") if st.get("needs_review") else ""),
        ]
        if st.get("review"):
            lines.append(t("import.review_codes", codes=", ".join(r["code"] for r in st["review"])))
        return Out(result, lines)
    lines = [t("import.imported", imported=result["imported"], rows=result["rows"], format=result["format"],
               currency=money.unit(result["currency"]), id=result["id"])]
    if result["skipped_duplicates"]:
        lines.append(t("import.skipped", count=result["skipped_duplicates"]))
    if result["months"]:
        lines.append(t("import.months", months=", ".join(result["months"])))
    return Out(result, lines)


@command("statements", "ekstreler", order=20, help="help.statements")
def statements(k: Kalan, a: argparse.Namespace) -> Out:
    items = k.statements()
    money = money_for(k)
    rows = [
        [s.id, shorten(f"{s.bank} {s.product}", 28), s.card_last4 or "", s.period_end.isoformat(),
         s.due_date.isoformat(), money(s.statement_total, s.currency), money(s.minimum_payment, s.currency),
         word("status", s.reconciliation.status if s.reconciliation else "none"), word("ai", s.analysis_status)]
        for s in items
    ]
    lines = table(cols("id", "card", "last4", "cut_off", "due", "total", "minimum", "check", "ai"), rows,
                  right=[5, 6])
    return Out({"items": [s.model_dump(mode="json") for s in items]}, lines or [t("statements.none")])


@command("banks", "bankalar", order=90, help="help.banks")
def banks(k: Kalan, a: argparse.Namespace) -> Out:
    ids = k.banks()
    return Out({"items": ids}, ids)


@command("teshis", "diagnose", order=95, help="help.teshis", args=[
    arg("file", help="help.teshis.file"),
    PASSWORD_ARG,
])
def diagnose(k: Kalan, a: argparse.Namespace) -> Out:
    result = k.diagnose(a.file, password=password(a))
    return Out(result["report"], raw=result["text"], notes=[t("diagnose.note")])
