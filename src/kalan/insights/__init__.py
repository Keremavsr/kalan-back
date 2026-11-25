""""What is this?" calculations: everything here is computed locally by code from stored data.

Each feature is a plain function (so the CLI and an MCP server can call it) plus a route in
:mod:`kalan.api.insights`. Rates and legal limits live in dated JSON files under ``data/`` with their
sources; the user can override the rates in settings (``PUT /insights/rules``).
"""
