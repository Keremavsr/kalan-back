"""``kalan mcp``: a read-only MCP server on stdio for the user's own AI CLI.

Claude Code, Codex or Gemini CLI start ``kalan mcp`` as a child process and can then answer questions
such as "where did I spend most this month?" from the local database. Every tool only reads: there
is no tool that imports, deletes, changes settings or calls an AI provider. stdout carries the MCP
protocol only; logs go to stderr.

The answers contain the same redacted data the web interface shows (descriptions, amounts, dates;
never card numbers or names). They go to whatever model the user's CLI talks to.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

import kalan
from kalan.service import Kalan, ServiceError, check_month

INSTRUCTIONS = """\
kalan is the user's local credit card statement tracker (installment-first). All tools are read-only.
Money: every amount is an integer in minor units of its currency (TRY 123456 means 1,234.56 TRY).
Months are YYYY-MM statement months: a transaction counts in the month of the statement that lists it.
Start with list_statements or monthly_summary; installment_schedule shows what is already committed for
the coming months; search_transactions finds rows by text, month or both.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)


def _call(fn, *args: Any) -> Any:
    try:
        return fn(*args)
    except ServiceError as exc:
        raise ToolError(f"{exc.message} ({exc.code})") from None


def _month(value: str | None) -> str | None:
    return _call(check_month, value)


def build_server(k: Kalan | None = None) -> MCPServer:
    k = k or Kalan()
    server = MCPServer("kalan", title="kalan", version=kalan.__version__, instructions=INSTRUCTIONS,
                       log_level="WARNING")

    @server.tool(annotations=READ_ONLY)
    def list_statements() -> dict[str, Any]:
        """Stored card statements, newest first: bank, last four card digits, period, due date, total,
        minimum payment, whether the printed totals reconciled, and whether an AI analysis exists."""
        return {"items": [s.model_dump(mode="json") for s in k.statements()]}

    @server.tool(annotations=READ_ONLY)
    def get_statement(statement_id: str) -> dict[str, Any]:
        """One statement with all its transactions (ids come from list_statements)."""
        return _call(k.statement, statement_id).model_dump(mode="json")

    @server.tool(annotations=READ_ONLY)
    def installment_schedule(months: int | None = None) -> dict[str, Any]:
        """Installments already committed for the coming months (default: the user's months_ahead setting,
        1-60): a total per month with its items, every open plan with what is left, and recurring fixed
        costs as a separate series."""
        if months is not None and not 1 <= months <= 60:
            raise ToolError("months must be between 1 and 60")
        return k.schedule(months).model_dump(mode="json")

    @server.tool(annotations=READ_ONLY)
    def monthly_summary(month: str | None = None) -> dict[str, Any]:
        """Spending of one statement month (YYYY-MM, default this month): total spend, credits, spend by
        category with shares, and the change against the previous month."""
        return k.month(_month(month))

    @server.tool(annotations=READ_ONLY)
    def alerts() -> dict[str, Any]:
        """Alerts computed in code: high limit use, payment due soon, interest, fees and taxes charged,
        foreign currency rows, missing statements, installments ending, subscription price rises."""
        return {"items": [a.model_dump(mode="json") for a in k.alerts()]}

    @server.tool(annotations=READ_ONLY)
    def subscriptions() -> dict[str, Any]:
        """Recurring charges (same merchant once a month) with the last and previous amount and the price
        change."""
        return {"items": k.subscriptions()}

    @server.tool(annotations=READ_ONLY)
    def search_transactions(query: str | None = None, month: str | None = None, limit: int = 50,
                            ) -> dict[str, Any]:
        """Transactions newest first. query matches the description, merchant or category (case and
        Turkish accents ignored); month (YYYY-MM) keeps one statement month; limit is 1-500."""
        if not 1 <= limit <= 500:
            raise ToolError("limit must be between 1 and 500")
        items = k.transactions(_month(month), query, limit)
        return {"items": items, "count": len(items)}

    return server


def run() -> int:
    build_server().run("stdio")
    return 0
