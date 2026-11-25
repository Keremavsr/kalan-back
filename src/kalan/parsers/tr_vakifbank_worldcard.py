"""VakıfBank Worldcard credit card statement (Turkey, TRY).

The layout lives in ``src/kalan/templates/tr_vakifbank_worldcard.yaml``; this module only keeps the
class name importable. The template reproduces the earlier hand-written parser exactly: every
ground-truth test in ``tests/test_vakifbank.py`` passes unchanged.
"""

from __future__ import annotations

from kalan.parsers.template import TemplateParser


class VakifbankWorldcard(TemplateParser):
    template_file = "tr_vakifbank_worldcard.yaml"
