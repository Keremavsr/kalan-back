"""``kalan kategori``: the user's own category rules (list, add, delete), as ``/category-rules``."""

from __future__ import annotations

import argparse

from kalan.ai.prompt import CATEGORIES
from kalan.commands import Out, arg, cols, command, table
from kalan.i18n import MESSAGES, t, word
from kalan.service import Kalan
from kalan.text import fold


def category_id(name: str) -> str:
    """A category id from its id or its Turkish or English name (``market`` -> ``groceries``)."""
    wanted = fold(name.strip())
    for cat in CATEGORIES:
        if wanted == fold(cat):
            return cat
    for cat in CATEGORIES:
        key = f"category.{cat}"
        if key in MESSAGES and any(wanted == fold(text) for text in MESSAGES[key] if text):
            return cat
    return name.strip()  # the service names the valid ones


@command("kategori", "categories", order=85, help="help.kategori", args=[
    arg("--add", "--ekle", nargs=2, metavar=("PATTERN", "CATEGORY"), help="help.kategori.add"),
    arg("--delete", "--sil", metavar="RULE_ID", help="help.kategori.delete"),
    arg("--no-apply", dest="apply", action="store_false", help="help.kategori.no_apply"),
    arg("--list-categories", "--kategoriler", dest="names", action="store_true", help="help.kategori.names"),
])
def rules(k: Kalan, a: argparse.Namespace) -> Out:
    if a.add:
        pattern, category = a.add
        r = k.add_category_rule(pattern, category_id(category), apply=a.apply)
        rule = r["rule"]
        lines = [t("rules.added", id=rule["id"], pattern=rule["pattern"], category=word("category", rule["category"]))]
        if a.apply:
            lines.append(t("rules.applied", count=r["applied"]))
        return Out(r, lines)
    if a.delete:
        k.delete_category_rule(a.delete)
        return Out({"deleted": a.delete}, [t("rules.deleted", id=a.delete)])
    data = k.category_rules()
    if a.names:
        rows = [[c, word("category", c)] for c in data["categories"]]
        return Out(data, table(cols("id", "name"), rows))
    rows = [[r["id"], r["pattern"], word("category", r["category"]), r["created_at"][:10]] for r in data["items"]]
    lines = table(cols("id", "pattern", "category", "date"), rows)
    return Out(data, [*lines, "", t("rules.hint")] if lines else [t("rules.none"), t("rules.hint")])
