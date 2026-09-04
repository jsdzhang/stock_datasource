"""Compact MCP tool surface for DeepSeek Harness and other context-limited clients.

Plugin query methods stay available by name, but models only see two tools:

- ``stock_list_tools`` — discover a plugin query by category or keyword
- ``stock_call_tool`` — invoke that query with JSON arguments

Results are truncated so a wide ClickHouse query cannot blow the model context.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
import os
from typing import Any

from fastmcp import FastMCP

from stock_datasource.core.service_generator import ServiceGenerator

logger = logging.getLogger(__name__)

CATEGORIES = (
    "market",
    "basic",
    "financial",
    "index",
    "etf",
    "hk",
    "realtime",
    "toplist",
    "other",
)

DEFAULT_LIST_LIMIT = 40
MAX_LIST_LIMIT = 200
DEFAULT_MAX_ROWS = 200
MAX_MAX_ROWS = 2000
DEFAULT_MAX_CHARS = 12_000

STOCK_LIST_TOOLS_NAME = "stock_list_tools"
STOCK_CALL_TOOL_NAME = "stock_call_tool"

DSH_SERVER_INSTRUCTIONS = (
    "A-share / HK / ETF / index market data. "
    "Always call stock_list_tools first, then stock_call_tool with the returned name. "
    "Prefer one ts_code and a short date range. Do not guess tool names."
)


def infer_category(plugin_name: str) -> str:
    """Map a plugin directory name onto a stable catalog category."""
    p = plugin_name.lower()
    if (
        p.startswith("akshare_hk")
        or p.startswith("tushare_hk")
        or "_hk_" in p
        or p.endswith("_hk")
    ):
        return "hk"
    if "etf" in p:
        return "etf"
    if p.startswith("tushare_rt_") or "_rt_" in p:
        return "realtime"
    if any(
        token in p
        for token in (
            "balancesheet",
            "income",
            "cashflow",
            "fina",
            "finace",
            "express",
            "forecast",
            "audit",
        )
    ):
        return "financial"
    if "daily_basic" in p or "index_dailybasic" in p:
        return "basic"
    if any(
        token in p
        for token in ("index", "idx_", "ths_index", "ths_member", "sw_daily", "ci_daily")
    ):
        return "index"
    if any(token in p for token in ("top_list", "top_inst", "stk_surv")):
        return "toplist"
    if any(
        token in p
        for token in (
            "stock_basic",
            "stock_company",
            "stock_st",
            "stock_hsgt",
            "trade_cal",
            "suspend",
            "stk_limit",
            "stk_rewards",
        )
    ):
        return "basic"
    if any(
        token in p
        for token in ("daily", "weekly", "monthly", "adj_factor", "cyq", "mins", "hsgt", "ggt")
    ):
        return "market"
    return "other"


def resolve_tool(
    generators: dict[str, ServiceGenerator], tool_name: str
) -> tuple[str, str, ServiceGenerator] | None:
    """Resolve ``{plugin}_{method}`` using the longest matching plugin prefix."""
    matches = [
        prefix
        for prefix in generators
        if tool_name.startswith(prefix + "_") and len(tool_name) > len(prefix) + 1
    ]
    if not matches:
        return None
    prefix = max(matches, key=len)
    method_name = tool_name[len(prefix) + 1 :]
    return prefix, method_name, generators[prefix]


def convert_arguments(
    generators: dict[str, ServiceGenerator],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Coerce JSON arguments to the Python types declared on the service method."""
    resolved = resolve_tool(generators, tool_name)
    if resolved is None:
        return dict(arguments)
    _prefix, method_name, generator = resolved
    method_info = generator.methods.get(method_name)
    if not method_info:
        return dict(arguments)

    type_hints = method_info.get("type_hints", {})
    converted: dict[str, Any] = {}
    for arg_name, arg_value in arguments.items():
        arg_type = type_hints.get(arg_name, str)
        origin = getattr(arg_type, "__origin__", None)
        if arg_type is list or origin is list:
            if isinstance(arg_value, str):
                converted[arg_name] = (
                    [part.strip() for part in arg_value.split(",")]
                    if "," in arg_value
                    else [arg_value]
                )
            elif isinstance(arg_value, list):
                converted[arg_name] = arg_value
            else:
                converted[arg_name] = [arg_value]
        elif arg_type is int:
            converted[arg_name] = int(arg_value) if isinstance(arg_value, str) else arg_value
        elif arg_type is float:
            converted[arg_name] = (
                float(arg_value) if isinstance(arg_value, str) else arg_value
            )
        elif arg_type is bool:
            if isinstance(arg_value, str):
                converted[arg_name] = arg_value.lower() in ("true", "1", "yes")
            else:
                converted[arg_name] = bool(arg_value)
        else:
            converted[arg_name] = arg_value
    return converted


def _sanitize(value: Any) -> Any:
    """Make a value JSON-serializable without NaN / pandas scalars."""
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if hasattr(value, "isoformat") and not isinstance(value, str):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if hasattr(value, "item") and not isinstance(value, (bytes, str, dict, list)):
        try:
            return _sanitize(value.item())
        except Exception:
            return str(value)
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    return str(value)


def _as_records(result: Any) -> Any:
    if hasattr(result, "to_dict") and hasattr(result, "columns"):
        try:
            return result.to_dict(orient="records")
        except TypeError:
            return result.to_dict()
    return result


def serialize_tool_result(
    result: Any,
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """JSON-encode a service result, truncating rows and characters."""
    max_rows = max(1, min(int(max_rows), MAX_MAX_ROWS))
    max_chars = max(500, int(max_chars))
    payload: dict[str, Any]
    records = _as_records(result)

    if isinstance(records, list):
        total = len(records)
        clipped = records[:max_rows]
        payload = {
            "status": "ok",
            "row_count": total,
            "returned": len(clipped),
            "truncated": total > len(clipped),
            "data": _sanitize(clipped),
        }
        if payload["truncated"]:
            payload["hint"] = (
                f"Truncated from {total} to {len(clipped)} rows. "
                "Narrow the query (ts_code / date range) or raise max_rows."
            )
    elif isinstance(records, dict):
        payload = {"status": "ok", "row_count": 1, "returned": 1, "truncated": False, "data": _sanitize(records)}
    else:
        payload = {
            "status": "ok",
            "row_count": 1,
            "returned": 1,
            "truncated": False,
            "data": _sanitize(records),
        }

    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return text

    data = payload.get("data")
    if isinstance(data, list) and data:
        kept = data
        while kept and len(text) > max_chars:
            kept = kept[: max(1, len(kept) // 2)]
            payload["data"] = kept
            payload["returned"] = len(kept)
            payload["truncated"] = True
            payload["hint"] = (
                f"Truncated to {len(kept)} rows / {max_chars} characters. "
                "Narrow the query."
            )
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if len(kept) == 1 and len(text) > max_chars:
                break
    if len(text) > max_chars:
        payload["data"] = None
        payload["truncated"] = True
        payload["hint"] = (
            f"Result exceeded {max_chars} characters even after row clipping. "
            "Narrow the query."
        )
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return text


def build_catalog_entries(
    generators: dict[str, ServiceGenerator],
) -> list[dict[str, Any]]:
    """Build the model-facing catalog from loaded service generators."""
    entries: list[dict[str, Any]] = []
    for prefix in sorted(generators):
        generator = generators[prefix]
        try:
            tools = generator.generate_mcp_tools()
        except Exception as exc:
            logger.warning("Failed to list tools for %s: %s", prefix, exc)
            continue
        category = infer_category(prefix)
        for tool in tools:
            method_name = tool.get("name") or ""
            if not method_name:
                continue
            entries.append(
                {
                    "name": f"{prefix}_{method_name}",
                    "plugin": prefix,
                    "method": method_name,
                    "category": category,
                    "description": tool.get("description") or "",
                    "inputSchema": tool.get("inputSchema")
                    or {"type": "object", "properties": {}},
                }
            )
    return entries


class ToolCatalog:
    """In-memory catalog over plugin query methods."""

    def __init__(self, generators: dict[str, ServiceGenerator]):
        self.generators = generators
        self.entries = build_catalog_entries(generators)

    def list_tools(
        self,
        category: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> str:
        limit = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
        category_filter = (category or "").strip().lower() or None
        query_filter = (query or "").strip().lower() or None

        if category_filter and category_filter not in CATEGORIES:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Unknown category: {category_filter}",
                    "categories": list(CATEGORIES),
                },
                ensure_ascii=False,
                indent=2,
            )

        matched: list[dict[str, Any]] = []
        for entry in self.entries:
            if category_filter and entry["category"] != category_filter:
                continue
            if query_filter:
                haystack = " ".join(
                    (
                        entry["name"],
                        entry["plugin"],
                        entry["method"],
                        entry["description"],
                    )
                ).lower()
                if query_filter not in haystack:
                    continue
            matched.append(
                {
                    "name": entry["name"],
                    "category": entry["category"],
                    "description": entry["description"],
                    "inputSchema": entry["inputSchema"],
                }
            )

        shown = matched[:limit]
        payload = {
            "status": "ok",
            "categories": list(CATEGORIES),
            "match_count": len(matched),
            "returned": len(shown),
            "truncated": len(matched) > len(shown),
            "next_step": (
                "Call stock_call_tool with one of the names below and arguments "
                "matching inputSchema."
            ),
            "tools": shown,
        }
        if not shown:
            payload["hint"] = (
                "No tools matched. Omit query, pick another category, "
                "or search for a plugin prefix such as tushare_daily."
            )
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def suggest_names(self, tool_name: str, limit: int = 5) -> list[str]:
        needle = (tool_name or "").lower()
        if not needle:
            return []
        hits: list[str] = []
        for entry in self.entries:
            name = entry["name"].lower()
            plugin = entry["plugin"].lower()
            if needle in name or needle in plugin or plugin in needle:
                hits.append(entry["name"])
        return hits[:limit]

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | str | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> str:
        name = (tool_name or "").strip()
        if not name:
            return json.dumps(
                {
                    "status": "error",
                    "error": "tool_name is required",
                    "hint": "Call stock_list_tools first.",
                },
                ensure_ascii=False,
            )

        parsed = arguments
        if parsed is None:
            parsed = {}
        if isinstance(parsed, str):
            raw = parsed.strip() or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"arguments is not valid JSON: {exc}",
                    },
                    ensure_ascii=False,
                )
        if not isinstance(parsed, dict):
            return json.dumps(
                {
                    "status": "error",
                    "error": "arguments must be a JSON object",
                },
                ensure_ascii=False,
            )

        resolved = resolve_tool(self.generators, name)
        if resolved is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Unknown tool: {name}",
                    "hint": "Call stock_list_tools to discover names.",
                    "suggestions": self.suggest_names(name),
                },
                ensure_ascii=False,
            )

        _prefix, method_name, generator = resolved
        handler = generator.get_tool_handler(method_name)
        if handler is None:
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Unknown tool: {name}",
                    "hint": "Call stock_list_tools to discover names.",
                    "suggestions": self.suggest_names(name),
                },
                ensure_ascii=False,
            )

        converted = convert_arguments(self.generators, name, parsed)
        signature = inspect.signature(handler)
        kwargs = {
            key: value
            for key, value in converted.items()
            if key in signature.parameters
        }
        missing = [
            param.name
            for param in signature.parameters.values()
            if param.name != "self"
            and param.default is inspect.Parameter.empty
            and param.name not in kwargs
        ]
        if missing:
            schema = next(
                (entry["inputSchema"] for entry in self.entries if entry["name"] == name),
                {},
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Missing required arguments: {', '.join(missing)}",
                    "inputSchema": schema,
                },
                ensure_ascii=False,
            )

        try:
            result = handler(**kwargs)
        except Exception as exc:
            logger.warning("stock_call_tool %s failed: %s", name, exc)
            return json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "tool_name": name,
                },
                ensure_ascii=False,
            )

        max_chars = int(os.environ.get("STOCK_MCP_MAX_CHARS", DEFAULT_MAX_CHARS))
        return serialize_tool_result(result, max_rows=max_rows, max_chars=max_chars)


def register_catalog_tools(
    server: FastMCP, generators: dict[str, ServiceGenerator]
) -> ToolCatalog:
    """Register the two catalog tools on an existing FastMCP server."""
    catalog = ToolCatalog(generators)

    @server.tool(
        name=STOCK_LIST_TOOLS_NAME,
        description=(
            "List stock data query tools. Filter with category "
            f"({', '.join(CATEGORIES)}) or a keyword such as daily / 茅台 / ts_code. "
            "Then call stock_call_tool with a returned name."
        ),
    )
    def stock_list_tools(
        category: str | None = None,
        query: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> str:
        return catalog.list_tools(category=category, query=query, limit=limit)

    @server.tool(
        name=STOCK_CALL_TOOL_NAME,
        description=(
            "Call a stock data query by exact tool name from stock_list_tools "
            "(for example tushare_daily_get_daily_data). "
            "Pass arguments as a JSON object matching that tool's inputSchema. "
            "Results are truncated; keep queries narrow."
        ),
    )
    def stock_call_tool(
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> str:
        return catalog.call_tool(tool_name, arguments=arguments, max_rows=max_rows)

    return catalog
