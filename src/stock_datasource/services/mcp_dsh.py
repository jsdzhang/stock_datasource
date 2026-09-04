"""DeepSeek Harness MCP entrypoint.

Default transport is stdio, which ``@deepseek-ai/dsh-mcp-client`` can spawn.
Optional native Streamable HTTP is for a long-lived process when DSH cannot
spawn this repo (use a different port from the legacy ``/messages`` server).

Logging goes to stderr so stdout stays a clean MCP byte stream.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys


def _configure_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stock datasource MCP server for DeepSeek Harness",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http", "streamable-http"),
        default=os.environ.get("STOCK_MCP_TRANSPORT", "stdio"),
        help="stdio for DSH spawn; streamable-http for a native MCP HTTP endpoint",
    )
    parser.add_argument("--host", default=os.environ.get("STOCK_MCP_DSH_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STOCK_MCP_DSH_PORT", "8002")),
    )
    parser.add_argument(
        "--path",
        default=os.environ.get("STOCK_MCP_DSH_PATH", "/mcp"),
        help="HTTP path for native Streamable HTTP (default /mcp)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    _configure_logging()
    args = build_parser().parse_args(argv)

    from stock_datasource.services.mcp_server import create_dsh_mcp_server

    server, generators = create_dsh_mcp_server()
    logging.getLogger(__name__).info(
        "DeepSeek Harness MCP ready (plugins=%d, transport=%s)",
        len(generators),
        args.transport,
    )

    if args.transport == "stdio":
        server.run(transport="stdio", show_banner=False)
        return

    http_transport = "streamable-http" if args.transport == "http" else args.transport
    server.run(
        transport=http_transport,
        host=args.host,
        port=args.port,
        path=args.path,
        show_banner=False,
    )


if __name__ == "__main__":
    main()
