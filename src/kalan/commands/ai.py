"""``kalan analiz``: AI commentary on one statement through the selected provider."""

from __future__ import annotations

import argparse
import textwrap

from kalan.commands import Out, arg, command
from kalan.service import Kalan


@command("analiz", "analyze", order=80, help="AI commentary on one statement (uses the selected AI provider)", args=[
    arg("statement_id", help="statement id (see `kalan statements`)"),
    arg("--cached", action="store_true", help="show the stored analysis, do not call the provider"),
])
def analyze(k: Kalan, a: argparse.Namespace) -> Out:
    result = k.analysis(a.statement_id) if a.cached else k.analyze(a.statement_id)
    commentary = result.get("commentary") or {}
    lines = textwrap.wrap(commentary.get("summary", ""), 100) or ["(no summary)"]
    for item in commentary.get("watch_items") or []:
        lines += ["", f"* {item.get('title', '')}", *textwrap.wrap(item.get("detail", ""), 98,
                                                                    initial_indent="  ", subsequent_indent="  ")]
    lines += ["", f"Provider: {result.get('provider')}, {result.get('created_at')}. Categories were applied."]
    return Out(result, lines)
