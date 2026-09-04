---
name: stock-mcp-query
description: Query historical stock market data (A-shares, HK stocks, ETFs, indices) via MCP. Use when the user wants daily K-line, financial statements, market indicators, screening, or other batch data. DeepSeek Harness clients should call stock_list_tools then stock_call_tool. Remote HTTP MCP on port 8001 still requires STOCK_MCP_TOKEN.
---

# Stock MCP Query

Query historical stock market data including daily OHLCV, financial reports, index data, ETF data, and more through the MCP protocol.

## Prerequisites

This skill requires:

1. **STOCK_MCP_TOKEN** environment variable — a JWT token purchased from the management platform (nps_enhanced)
2. **STOCK_MCP_SERVER_URL** environment variable (optional) — the MCP server URL, defaults to the URL provided at purchase time

## Setup Check

Before using any data query tools, verify the environment is configured:

```bash
# Check if token exists
echo ${STOCK_MCP_TOKEN:+Token is set}${STOCK_MCP_TOKEN:-ERROR: STOCK_MCP_TOKEN not set}
```

If `STOCK_MCP_TOKEN` is not set:

1. Visit the management platform (nps_enhanced web panel)
2. Navigate to "MCP Data Query" subscription page
3. Purchase a query quota pack (e.g., 10k records for 10 CNY)
4. Copy the issued token
5. Set the environment variable:
   ```bash
   export STOCK_MCP_TOKEN="eyJ..."
   export STOCK_MCP_SERVER_URL="https://your-node:8001/messages"
   ```

## DeepSeek Harness

When running inside DeepSeek Harness (`dsh`), do **not** call the raw plugin tools. Use:

1. `mcp__stock__stock_list_tools` — `category` is one of `market|basic|financial|index|etf|hk|realtime|toplist|other`; `query` is a keyword such as `daily` or a ts_code prefix.
2. `mcp__stock__stock_call_tool` — `tool_name` must be a `name` returned by the list tool (example: `tushare_daily_get_daily_data`); `arguments` is a JSON object matching that tool's `inputSchema`.

Keep queries narrow (one `ts_code` / `code` and a short date range). Results are truncated.

Start dsh with this repo's overlay instead of pointing at `localhost:8001/messages`:

```bash
export STOCK_DATASOURCE_ROOT=/absolute/path/to/stock_datasource
dsh web --patch "$STOCK_DATASOURCE_ROOT/integrations/dsh/stock.cordis.yml"
```

Stdio MCP spawned by dsh does not use `STOCK_MCP_TOKEN`.

## MCP Server Configuration (Claude Code / Cursor / PicoClaw)

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "stock-data": {
      "type": "streamable-http",
      "url": "${STOCK_MCP_SERVER_URL}",
      "headers": {
        "Authorization": "Bearer ${STOCK_MCP_TOKEN}"
      }
    }
  }
}
```

## Available Data Categories

| Category | Description | Example Tools |
|----------|-------------|---------------|
| Daily K-line | OHLCV + volume + turnover | `tushare_daily_*` |
| Daily Basics | PE, PB, market cap, volume ratio | `tushare_daily_basic_*` |
| Adj Factor | Forward/backward adjustment factors | `tushare_adj_factor_*` |
| Financial | Balance sheet, income, cash flow | `tushare_balancesheet_*`, etc. |
| Index | Index daily, weights, components | `tushare_index_*` |
| ETF | ETF daily prices and holdings | `tushare_fund_*` |
| HK Stock | Hong Kong stock daily data | `akshare_hk_*` |
| Market | Market overview, trading calendar | `tushare_trade_cal_*` |

## Usage Notes

- Each tool call returns data and counts against your query quota
- The response includes `_usage.record_count` showing records returned
- The response includes `_usage.quota_remaining` showing remaining quota
- When quota is exhausted, purchase additional packs from the management platform
- Data is sourced from ClickHouse and covers the full available history

## Billing

- Billed per record (data row) returned by each tool call
- Query packs are valid for 90 days from purchase
- Multiple packs stack additively (quota accumulates)
- Usage is reconciled asynchronously between the MCP server and management platform

## Troubleshooting

- **401 "API key required"**: `STOCK_MCP_TOKEN` is not set or not passed in Authorization header
- **401 "Token expired"**: Your token has expired. Purchase a new quota pack to get a fresh token
- **401 "Invalid token"**: Token is malformed or the server's public key doesn't match
- **No data returned**: Check that the requested date is a trading day, or the stock code is valid
- **Quota exhausted**: The `_usage.quota_remaining` will show 0. Purchase additional packs
