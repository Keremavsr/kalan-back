"""``kalan analiz``: AI commentary on one statement through the selected provider."""

from __future__ import annotations

import argparse
import textwrap

from kalan.commands import Out, arg, command
from kalan.i18n import t
from kalan.service import Kalan


@command("analiz", "analyze", order=80, help="help.analiz", args=[
    arg("statement_id", help="help.analiz.id"),
    arg("--cached", action="store_true", help="help.analiz.cached"),
])
def analyze(k: Kalan, a: argparse.Namespace) -> Out:
    result = k.analysis(a.statement_id) if a.cached else k.analyze(a.statement_id)
    commentary = result.get("commentary") or {}
    lines = textwrap.wrap(commentary.get("summary", ""), 100) or [t("analyze.no_summary")]
    for item in commentary.get("watch_items") or []:
        lines += ["", f"* {item.get('title', '')}", *textwrap.wrap(item.get("detail", ""), 98,
                                                                    initial_indent="  ", subsequent_indent="  ")]
    lines += ["", t("analyze.footer", provider=result.get("provider"), created=result.get("created_at"))]
    return Out(result, lines)
