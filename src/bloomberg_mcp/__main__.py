"""Entry point: `python -m bloomberg_mcp` or `bloomberg-mcp`."""

from __future__ import annotations

import argparse
import logging

from .config import settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="bloomberg-mcp", description="Bloomberg MCP server")
    parser.add_argument(
        "--transport",
        choices=("http", "stdio"),
        default="http",
        help="http serves streamable HTTP at MCP_PATH (default); stdio is for local clients",
    )
    parser.add_argument("--host", default=settings.http_host)
    parser.add_argument("--port", type=int, default=settings.http_port)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    from . import server

    if args.transport == "stdio":
        server.mcp.run(transport="stdio")
        return

    import uvicorn

    settings.http_host, settings.http_port = args.host, args.port
    logging.getLogger(__name__).info(
        "MCP endpoint: http://%s:%s%s  (stateless=%s, json_response=%s)",
        args.host,
        args.port,
        settings.mcp_path,
        settings.stateless,
        settings.json_response,
    )
    uvicorn.run(server.build_app(), host=args.host, port=args.port, log_level=settings.log_level)


if __name__ == "__main__":
    main()
