"""``kalan mcp``: read-only tools over the same service the CLI uses."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from kalan.config import Config
from kalan.mcp_server import build_server
from kalan.service import Kalan
from kalan.store import Store

GB_PDFS = sorted((Path(__file__).parent / "fixtures" / "garanti").glob("gb-*.pdf"))
TOOLS = {"list_statements", "get_statement", "installment_schedule", "monthly_summary", "alerts", "subscriptions",
         "search_transactions", "minimum_payment", "which_card", "next_statement_forecast", "early_payoff",
         "card_fees"}


@pytest.fixture
def kalan(tmp_path) -> Kalan:
    config = Config(data_dir=tmp_path)
    k = Kalan(config=config, store=Store(config.db_path))
    for pdf in GB_PDFS[:4]:
        k.import_file(pdf, ai_fallback=False)
    return k


def call(server, name: str, args: dict | None = None):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args or {})

    return asyncio.run(go())


def data(result) -> dict:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def test_tools_are_exactly_the_read_only_set(kalan):
    async def go():
        async with Client(build_server(kalan)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(go())
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False


def test_tools_return_the_service_data(kalan):
    server = build_server(kalan)
    statements = data(call(server, "list_statements"))["items"]
    assert [s["id"] for s in statements] == [s.id for s in kalan.statements()]
    one = data(call(server, "get_statement", {"statement_id": statements[0]["id"]}))
    assert one["transactions"] and one["id"] == statements[0]["id"]
    assert data(call(server, "installment_schedule", {"months": 3})) == kalan.schedule(3).model_dump(mode="json")
    assert data(call(server, "monthly_summary", {"month": "2026-02"})) == kalan.month("2026-02")
    assert data(call(server, "alerts"))["items"] == [a.model_dump(mode="json") for a in kalan.alerts()]
    assert data(call(server, "subscriptions"))["items"] == kalan.subscriptions()
    found = data(call(server, "search_transactions", {"query": "ornekmarket", "limit": 5}))
    assert found["count"] == len(found["items"]) > 0
    assert all("ORNEKMARKET" in t["description"] for t in found["items"])


def test_insight_tools(kalan):
    server = build_server(kalan)
    minimum = data(call(server, "minimum_payment", {"cycles": 3}))
    full = kalan.minimum_payment()
    assert len(minimum["cycles"]) == 3 and minimum["cycles_total"] == len(full["cycles"])
    assert minimum["minimum_only"] == full["minimum_only"]
    assert data(call(server, "which_card", {"date": "2026-05-01"})) == kalan.which_card(date(2026, 5, 1))
    assert data(call(server, "next_statement_forecast"))["forecast"] == kalan.forecast()["forecast"]
    with_bt = data(call(server, "next_statement_forecast", {"backtest_months": True}))
    assert "months" in with_bt["backtest"]
    assert data(call(server, "early_payoff")) == kalan.early_payoff()
    assert data(call(server, "card_fees"))["items"] == kalan.card_fees()


def test_tool_errors(kalan):
    server = build_server(kalan)
    for name, args in [("get_statement", {"statement_id": "st_missing"}), ("monthly_summary", {"month": "2026-13"}),
                       ("installment_schedule", {"months": 0}), ("search_transactions", {"limit": 0}),
                       ("which_card", {"date": "2026-13-01"}), ("early_payoff", {"plan_id": "pl_missing"}),
                       ("minimum_payment", {"statement_id": "st_missing"})]:
        assert call(server, name, args).is_error, name


def test_nothing_was_written(kalan):
    before = kalan.config.db_path.read_bytes()
    server = build_server(kalan)
    for name in TOOLS - {"get_statement"}:
        call(server, name)
    call(server, "next_statement_forecast", {"backtest_months": True})
    call(server, "minimum_payment", {"include_installments": True})
    assert kalan.config.db_path.read_bytes() == before


def test_stdio_subprocess(kalan):
    env = {**os.environ, "KALAN_DATA_DIR": str(kalan.config.data_dir)}
    params = StdioServerParameters(command=sys.executable, args=["-m", "kalan.cli", "mcp"], env=env)

    async def go():
        async with Client(params, read_timeout_seconds=60) as client:
            return await client.call_tool("list_statements", {})

    assert len(data(asyncio.run(go()))["items"]) == 4
