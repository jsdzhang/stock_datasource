"""Tests for the DeepSeek Harness MCP catalog surface."""

from __future__ import annotations

import json
from typing import Any

import pytest

from stock_datasource.services.mcp_catalog import (
    STOCK_CALL_TOOL_NAME,
    STOCK_LIST_TOOLS_NAME,
    ToolCatalog,
    infer_category,
    register_catalog_tools,
    resolve_tool,
    serialize_tool_result,
)
from stock_datasource.services.mcp_dsh import build_parser


class FakeGenerator:
    """Minimal ServiceGenerator stand-in for catalog tests."""

    def __init__(
        self,
        tools: list[dict[str, Any]],
        handlers: dict[str, Any],
        type_hints: dict[str, dict[str, type]] | None = None,
    ):
        self._tools = tools
        self._handlers = handlers
        hints = type_hints or {}
        self.methods = {
            tool["name"]: {
                "type_hints": hints.get(tool["name"], {}),
                "metadata": {"description": tool.get("description", ""), "params": []},
            }
            for tool in tools
        }

    def generate_mcp_tools(self) -> list[dict[str, Any]]:
        return self._tools

    def get_tool_handler(self, name: str):
        return self._handlers.get(name)


def _daily_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Stock code"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
        "required": ["code", "start_date", "end_date"],
    }


def _sample_generators() -> dict[str, FakeGenerator]:
    def get_daily_data(code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        return [
            {"ts_code": code, "trade_date": start_date, "close": 100.0},
            {"ts_code": code, "trade_date": end_date, "close": 101.0},
        ]

    def get_latest_daily(code: str) -> dict[str, Any]:
        return {"ts_code": code, "close": 101.0}

    def get_basic(ts_code: str) -> dict[str, Any]:
        return {"ts_code": ts_code, "pe": 12.3}

    return {
        "tushare_daily": FakeGenerator(
            tools=[
                {
                    "name": "get_daily_data",
                    "description": "Query daily stock data by code and date range",
                    "inputSchema": _daily_schema(),
                },
                {
                    "name": "get_latest_daily",
                    "description": "Latest daily bar",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                        "required": ["code"],
                    },
                },
            ],
            handlers={
                "get_daily_data": get_daily_data,
                "get_latest_daily": get_latest_daily,
            },
            type_hints={
                "get_daily_data": {
                    "code": str,
                    "start_date": str,
                    "end_date": str,
                },
                "get_latest_daily": {"code": str},
            },
        ),
        "tushare_daily_basic": FakeGenerator(
            tools=[
                {
                    "name": "get_daily_basic",
                    "description": "PE/PB daily basics",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"ts_code": {"type": "string"}},
                        "required": ["ts_code"],
                    },
                }
            ],
            handlers={"get_daily_basic": get_basic},
            type_hints={"get_daily_basic": {"ts_code": str}},
        ),
    }


class TestInferCategory:
    @pytest.mark.parametrize(
        ("plugin", "category"),
        [
            ("tushare_daily", "market"),
            ("tushare_weekly", "market"),
            ("tushare_daily_basic", "basic"),
            ("tushare_stock_basic", "basic"),
            ("tushare_income", "financial"),
            ("tushare_balancesheet_vip", "financial"),
            ("tushare_index_daily", "index"),
            ("tushare_etf_fund_daily", "etf"),
            ("akshare_hk_daily", "hk"),
            ("tushare_hk_income", "hk"),
            ("tushare_rt_k", "realtime"),
            ("tushare_top_list", "toplist"),
        ],
    )
    def test_known_plugins(self, plugin: str, category: str):
        assert infer_category(plugin) == category


class TestResolveTool:
    def test_longest_prefix_wins(self):
        generators = _sample_generators()
        resolved = resolve_tool(generators, "tushare_daily_basic_get_daily_basic")
        assert resolved is not None
        prefix, method, _gen = resolved
        assert prefix == "tushare_daily_basic"
        assert method == "get_daily_basic"

    def test_shorter_plugin_still_matches(self):
        generators = _sample_generators()
        resolved = resolve_tool(generators, "tushare_daily_get_daily_data")
        assert resolved is not None
        prefix, method, _gen = resolved
        assert prefix == "tushare_daily"
        assert method == "get_daily_data"

    def test_unknown_name(self):
        assert resolve_tool(_sample_generators(), "no_such_tool") is None


class TestSerializeResult:
    def test_truncates_rows(self):
        rows = [{"i": i} for i in range(50)]
        payload = json.loads(serialize_tool_result(rows, max_rows=10, max_chars=50_000))
        assert payload["status"] == "ok"
        assert payload["row_count"] == 50
        assert payload["returned"] == 10
        assert payload["truncated"] is True

    def test_truncates_characters(self):
        rows = [{"blob": "x" * 400} for _ in range(40)]
        payload = json.loads(serialize_tool_result(rows, max_rows=40, max_chars=800))
        assert payload["truncated"] is True
        assert payload["data"] is None or (
            isinstance(payload["data"], list) and len(payload["data"]) < 40
        )


class TestToolCatalog:
    def test_list_by_category(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(catalog.list_tools(category="market"))
        names = {item["name"] for item in payload["tools"]}
        assert "tushare_daily_get_daily_data" in names
        assert "tushare_daily_basic_get_daily_basic" not in names

    def test_list_by_query(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(catalog.list_tools(query="pe/pb"))
        names = {item["name"] for item in payload["tools"]}
        assert names == {"tushare_daily_basic_get_daily_basic"}

    def test_unknown_category(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(catalog.list_tools(category="crypto"))
        assert payload["status"] == "error"

    def test_call_success(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(
            catalog.call_tool(
                "tushare_daily_get_daily_data",
                {
                    "code": "600519.SH",
                    "start_date": "20260101",
                    "end_date": "20260105",
                },
            )
        )
        assert payload["status"] == "ok"
        assert payload["row_count"] == 2
        assert payload["data"][0]["ts_code"] == "600519.SH"

    def test_call_uses_longest_prefix(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(
            catalog.call_tool(
                "tushare_daily_basic_get_daily_basic",
                {"ts_code": "600519.SH"},
            )
        )
        assert payload["status"] == "ok"
        assert payload["data"]["pe"] == 12.3

    def test_call_missing_args(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(catalog.call_tool("tushare_daily_get_daily_data", {}))
        assert payload["status"] == "error"
        assert "code" in payload["error"]

    def test_call_unknown_tool(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(catalog.call_tool("tushare_daily_get_missing", {}))
        assert payload["status"] == "error"
        assert payload["suggestions"]

    def test_arguments_json_string(self):
        catalog = ToolCatalog(_sample_generators())
        payload = json.loads(
            catalog.call_tool(
                "tushare_daily_get_latest_daily",
                '{"code": "000001.SZ"}',
            )
        )
        assert payload["status"] == "ok"
        assert payload["data"]["ts_code"] == "000001.SZ"


@pytest.mark.asyncio
async def test_fastmcp_catalog_roundtrip():
    from fastmcp import Client, FastMCP

    server = FastMCP("stock-datasource-test")
    register_catalog_tools(server, _sample_generators())
    async with Client(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert names == {STOCK_LIST_TOOLS_NAME, STOCK_CALL_TOOL_NAME}

        listed = await client.call_tool(
            STOCK_LIST_TOOLS_NAME, {"category": "market", "limit": 10}
        )
        listed_text = listed.content[0].text
        listed_payload = json.loads(listed_text)
        assert listed_payload["status"] == "ok"
        assert listed_payload["tools"]

        called = await client.call_tool(
            STOCK_CALL_TOOL_NAME,
            {
                "tool_name": "tushare_daily_get_daily_data",
                "arguments": {
                    "code": "600519.SH",
                    "start_date": "20260101",
                    "end_date": "20260102",
                },
            },
        )
        called_payload = json.loads(called.content[0].text)
        assert called_payload["status"] == "ok"
        assert called_payload["row_count"] == 2


@pytest.mark.asyncio
async def test_create_dsh_mcp_server_only_exposes_catalog_tools(monkeypatch):
    from fastmcp import Client
    from stock_datasource.services import mcp_server as mcp_server_mod

    monkeypatch.setattr(mcp_server_mod, "load_service_generators", _sample_generators)
    server, generators = mcp_server_mod.create_dsh_mcp_server()
    assert "tushare_daily" in generators
    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == {STOCK_LIST_TOOLS_NAME, STOCK_CALL_TOOL_NAME}


def test_dsh_cli_defaults():
    parser = build_parser()
    args = parser.parse_args([])
    assert args.transport == "stdio"
    assert args.port == 8002
    assert args.path == "/mcp"


def test_main_stdio_invokes_fastmcp_run(monkeypatch):
    called: dict[str, Any] = {}

    class FakeServer:
        def run(self, **kwargs):
            called.update(kwargs)

    monkeypatch.setattr(
        "stock_datasource.services.mcp_server.create_dsh_mcp_server",
        lambda: (FakeServer(), {"tushare_daily": object()}),
    )
    from stock_datasource.services.mcp_dsh import main

    main([])
    assert called["transport"] == "stdio"
    assert called["show_banner"] is False


@pytest.mark.asyncio
async def test_live_dsh_server_lists_real_daily_tool():
    from fastmcp import Client
    from stock_datasource.services.mcp_server import create_dsh_mcp_server

    server, generators = create_dsh_mcp_server()
    if "tushare_daily" not in generators:
        pytest.skip("tushare_daily service failed to initialize")

    async with Client(server) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert names == {STOCK_LIST_TOOLS_NAME, STOCK_CALL_TOOL_NAME}
        listed = await client.call_tool(
            STOCK_LIST_TOOLS_NAME,
            {"query": "tushare_daily_get_daily_data", "limit": 5},
        )
        payload = json.loads(listed.content[0].text)
        assert payload["status"] == "ok"
        assert any(
            item["name"] == "tushare_daily_get_daily_data" for item in payload["tools"]
        )


def test_real_plugins_are_in_catalog():
    from stock_datasource.services.mcp_server import load_service_generators

    generators = load_service_generators()
    if "tushare_daily" not in generators:
        pytest.skip("tushare_daily service failed to initialize")
    catalog = ToolCatalog(generators)
    payload = json.loads(catalog.list_tools(query="tushare_daily_get_daily_data"))
    names = {item["name"] for item in payload["tools"]}
    assert "tushare_daily_get_daily_data" in names
