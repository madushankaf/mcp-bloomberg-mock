"""Smoke test: connect to the running server over streamable HTTP and call every tool.

    uv run python scripts/smoke_client.py [http://localhost:8000/mcp]
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/mcp"
HEADERS = {"x-api-key": os.environ["MCP_API_KEY"]} if os.getenv("MCP_API_KEY") else None

CALLS = [
    ("bloomberg_status", {}),
    (
        "get_reference_data",
        {
            "securities": ["IBM US Equity", "AAPL US Equity"],
            "fields": ["NAME", "PX_LAST", "CUR_MKT_CAP", "CRNCY"],
        },
    ),
    (
        "get_historical_data",
        {
            "securities": ["AAPL US Equity"],
            "fields": ["PX_LAST", "VOLUME"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-10",
        },
    ),
    ("search_instruments", {"query": "apple", "yellow_key": "EQTY", "max_results": 3}),
    ("search_fields", {"query": "market cap"}),
    (
        "get_intraday_bars",
        {
            "security": "IBM US Equity",
            "start_datetime": "2024-01-15T14:30:00",
            "end_datetime": "2024-01-15T15:00:00",
            "interval_minutes": 15,
        },
    ),
    (
        "get_intraday_ticks",
        {
            "security": "IBM US Equity",
            "start_datetime": "2024-01-15T14:30:00",
            "end_datetime": "2024-01-15T14:32:00",
        },
    ),
    ("run_equity_screen", {"screen_name": "Top Movers"}),
]


def render(result):
    if result.is_error:
        return "ERROR: " + " ".join(c.text for c in result.content if c.type == "text")
    payload = result.structured_content or {
        "text": [c.text for c in result.content if c.type == "text"]
    }
    text = json.dumps(payload, indent=2)
    return text if len(text) < 900 else text[:900] + "\n  ... truncated"


async def main() -> int:
    http_client = create_mcp_http_client(headers=HEADERS) if HEADERS else None
    async with streamable_http_client(URL, http_client=http_client) as (read, write, *_):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"connected: {info.server_info.name} v{info.server_info.version}")

            tools = await session.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))

            resources = await session.list_resources()
            print("resources:", ", ".join(str(r.uri) for r in resources.resources))

            failures = 0
            for name, args in CALLS:
                result = await session.call_tool(name, args)
                failures += bool(result.is_error)
                print(f"\n=== {name} ===\n{render(result)}")
            print(f"\n{len(CALLS) - failures}/{len(CALLS)} tool calls succeeded")
            return 1 if failures else 0


sys.exit(asyncio.run(main()))
