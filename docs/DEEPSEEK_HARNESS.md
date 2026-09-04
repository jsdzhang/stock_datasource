# DeepSeek Harness 接入

把本仓库的行情/财务查询接到 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）。stock_datasource 只作为 MCP 数据服务；Agent 循环仍由 dsh 运行。

本仓库文档里另有一份 LangGraph `create_deep_agent` 的「Harness 迁移」说明，和 DeepSeek Harness **不是同一件事**。

## 为什么不用现有 8001 `/messages`

dsh 的 `@deepseek-ai/dsh-mcp-client` 走官方 MCP SDK（stdio 或 Streamable HTTP）。现有 `http://localhost:8001/messages` 是给 PicoClaw / 部分 IDE 用的自研 JSON-RPC，协议字段与官方 Streamable HTTP 不一致。DeepSeek Harness 请使用下面的专用入口。

## 模型能看到的工具

插件查询方法有上百个。直接全部暴露会撑爆 dsh 上下文，因此专用入口只注册两个工具，公开名在 dsh 里为：

| dsh 工具名 | 作用 |
|---|---|
| `mcp__stock__stock_list_tools` | 按 `category` / 关键词列出可调用查询 |
| `mcp__stock__stock_call_tool` | 按 `stock_list_tools` 返回的 `name` 调用，结果自动截断 |

`category`：`market`、`basic`、`financial`、`index`、`etf`、`hk`、`realtime`、`toplist`、`other`。

示例：

1. `stock_list_tools(category="market", query="daily")`
2. `stock_call_tool(tool_name="tushare_daily_get_daily_data", arguments={"code":"600519.SH","start_date":"20260101","end_date":"20260131"})`

## 推荐：stdio（dsh 拉起本仓库）

前置：本机已安装 `uv`、Node 版 `dsh`，且本仓库 `.env` 能连上 ClickHouse。

```bash
export STOCK_DATASOURCE_ROOT=/absolute/path/to/stock_datasource
# `--patch` 是 dsh 启动器参数，必须写在 `--no-open` / `--port` 前面。
dsh web --patch "$STOCK_DATASOURCE_ROOT/integrations/dsh/stock.cordis.yml"
```

overlay 会：

- 用 `uv run --directory $STOCK_DATASOURCE_ROOT python -m stock_datasource.services.mcp_dsh` 拉起 stdio MCP
- 把仓库 `skills/` 挂到 dsh 的 skill 目录（`stock-mcp-query` 等）

手动确认进程本身可用：

```bash
cd "$STOCK_DATASOURCE_ROOT"
uv run python -c "
import asyncio, json
from fastmcp import Client
from stock_datasource.services.mcp_server import create_dsh_mcp_server

async def main():
    server, _ = create_dsh_mcp_server()
    async with Client(server) as client:
        tools = await client.list_tools()
        print([t.name for t in tools])
        listed = await client.call_tool('stock_list_tools', {'query': 'tushare_daily_get_daily', 'limit': 5})
        print(listed.content[0].text[:500])

asyncio.run(main())
"
```

在 dsh 会话里问：「用 stock 工具查 600519.SH 最近几个交易日收盘价」。应先出现 `mcp__stock__stock_list_tools`，再出现 `mcp__stock__stock_call_tool`。

## 备选：本机常驻 Streamable HTTP

当 dsh 不能 spawn 本仓库（无 `uv`、工作目录不是本仓库）时，先单独起官方 HTTP：

```bash
uv run python -m stock_datasource.services.mcp_dsh --transport streamable-http --host 127.0.0.1 --port 8002
```

默认路径 `/mcp`，与 8001 上的 legacy `/messages` 互不干扰。然后：

```bash
export STOCK_MCP_DSH_URL=http://127.0.0.1:8002/mcp
dsh web --patch "$STOCK_DATASOURCE_ROOT/integrations/dsh/stock-http.cordis.yml"
```

## 配置

| 变量 | 默认 | 含义 |
|---|---|---|
| `STOCK_DATASOURCE_ROOT` | dsh 进程 cwd | 本仓库绝对路径，stdio overlay 必填（除非就在仓库根目录启动 dsh） |
| `STOCK_MCP_TRANSPORT` | `stdio` | `stdio` / `streamable-http` |
| `STOCK_MCP_DSH_HOST` | `127.0.0.1` | HTTP 监听地址 |
| `STOCK_MCP_DSH_PORT` | `8002` | HTTP 端口 |
| `STOCK_MCP_DSH_PATH` | `/mcp` | HTTP 路径 |
| `STOCK_MCP_DSH_URL` | `http://127.0.0.1:8002/mcp` | HTTP overlay 的 MCP URL |
| `STOCK_MCP_MAX_ROWS` | 调用参数 `max_rows`（默认 200） | 单次返回行数上限 |
| `STOCK_MCP_MAX_CHARS` | `12000` | 单次返回字符上限 |
| `LOG_LEVEL` | `INFO` | stdio 日志打到 stderr |

stdio 本地进程不走 8001 的 Bearer 鉴权。不要把该进程暴露到公网。需要鉴权的远程访问继续用开放 API 网关或 legacy MCP API Key。

## 相关文件

- `src/stock_datasource/services/mcp_dsh.py` — 入口
- `src/stock_datasource/services/mcp_catalog.py` — 两层工具与截断
- `integrations/dsh/stock.cordis.yml` — stdio overlay
- `integrations/dsh/stock-http.cordis.yml` — HTTP overlay
- `skills/stock-mcp-query/SKILL.md` — 查询约定
