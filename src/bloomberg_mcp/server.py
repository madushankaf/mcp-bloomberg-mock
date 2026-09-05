"""FastMCP server exposing Bloomberg data over streamable HTTP at /mcp."""

from __future__ import annotations

import logging
from typing import Any

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .backend import Backend, BloombergError
from .config import Settings, settings

log = logging.getLogger(__name__)

_backend: Backend | None = None


def get_backend(config: Settings = settings) -> Backend:
    """Pick a backend once per process: real blpapi, or the mock."""
    global _backend
    if _backend is not None:
        return _backend

    from .mock_backend import MockBackend

    if config.mode == "mock":
        _backend = MockBackend(config)
        log.warning("Bloomberg backend: MOCK (BLOOMBERG_MODE=mock)")
        return _backend

    from .blpapi_backend import BlpapiBackend

    candidate = BlpapiBackend(config)
    try:
        candidate._ensure_session()  # connect eagerly so failures surface at startup
        _backend = candidate
        log.info("Bloomberg backend: BLPAPI %s:%s", config.bbg_host, config.bbg_port)
        return _backend
    except BloombergError as exc:
        if config.mode == "blpapi":
            raise
        log.warning("Bloomberg unavailable (%s) - falling back to MOCK data", exc)
        _backend = MockBackend(config, reason=str(exc))
        return _backend


async def _call(method: str, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking backend call off the event loop."""
    backend = await anyio.to_thread.run_sync(get_backend)
    return await anyio.to_thread.run_sync(lambda: getattr(backend, method)(*args, **kwargs))


def _check_list(name: str, values: list[str], limit: int) -> list[str]:
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        raise BloombergError(f"{name} must not be empty")
    if len(cleaned) > limit:
        raise BloombergError(f"Too many {name}: {len(cleaned)} > limit {limit}")
    return cleaned


mcp = MCPServer(
    name="bloomberg",
    version=__version__,
    instructions=(
        "Bloomberg market and reference data via the Bloomberg Open API (BLPAPI).\n"
        "Securities use Bloomberg identifiers with a yellow key, e.g. 'IBM US Equity', "
        "'SPX Index', 'EURUSD Curncy', 'USGG10YR Index'. Fields are Bloomberg mnemonics, "
        "e.g. PX_LAST, PX_OPEN, VOLUME, CUR_MKT_CAP, NAME, CRNCY.\n"
        "Use search_instruments to resolve a name to a ticker and search_fields to find a "
        "field mnemonic before calling the data tools. Check bloomberg_status first if a "
        "call fails - the server may be serving mock data."
    ),
)


@mcp.tool()
async def bloomberg_status() -> dict[str, Any]:
    """Report which backend is live (real BLPAPI vs mock) and its connection details.

    Call this first when data looks wrong, or to confirm the server is talking to a
    real Bloomberg Terminal / SAPI / B-PIPE endpoint.
    """
    return await _call("info")


@mcp.tool()
async def get_reference_data(
    securities: list[str],
    fields: list[str],
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Current/static data for one or more securities (BLPAPI ReferenceDataRequest).

    This is the equivalent of the Excel BDP() function.

    Args:
        securities: Bloomberg tickers, e.g. ["IBM US Equity", "SPX Index"].
        fields: Field mnemonics, e.g. ["PX_LAST", "NAME", "CUR_MKT_CAP"].
        overrides: Optional field overrides, e.g. {"BEST_FPERIOD_OVERRIDE": "1FY"}.
    """
    return await _call(
        "reference_data",
        _check_list("securities", securities, settings.max_securities),
        _check_list("fields", fields, settings.max_fields),
        overrides,
    )


@mcp.tool()
async def get_historical_data(
    securities: list[str],
    fields: list[str],
    start_date: str,
    end_date: str,
    periodicity: str = "DAILY",
    currency: str | None = None,
    max_data_points: int | None = None,
) -> dict[str, Any]:
    """End-of-period historical time series (BLPAPI HistoricalDataRequest, i.e. BDH()).

    Args:
        securities: Bloomberg tickers, e.g. ["AAPL US Equity"].
        fields: Field mnemonics, e.g. ["PX_LAST", "VOLUME"].
        start_date: YYYY-MM-DD.
        end_date: YYYY-MM-DD.
        periodicity: DAILY | WEEKLY | MONTHLY | QUARTERLY | SEMI_ANNUALLY | YEARLY.
        currency: Optional ISO code to convert into, e.g. "USD".
        max_data_points: Optional cap on returned points (most recent are kept).
    """
    return await _call(
        "historical_data",
        _check_list("securities", securities, settings.max_securities),
        _check_list("fields", fields, settings.max_fields),
        start_date,
        end_date,
        periodicity,
        currency,
        True,
        max_data_points,
    )


@mcp.tool()
async def get_intraday_bars(
    security: str,
    start_datetime: str,
    end_datetime: str,
    event_type: str = "TRADE",
    interval_minutes: int = 5,
) -> dict[str, Any]:
    """Intraday OHLCV bars for a single security (BLPAPI IntradayBarRequest).

    Bloomberg keeps roughly 140 days of intraday history.

    Args:
        security: A single Bloomberg ticker, e.g. "IBM US Equity".
        start_datetime: ISO-8601, interpreted as UTC, e.g. "2024-01-15T13:30:00".
        end_datetime: ISO-8601, interpreted as UTC.
        event_type: TRADE | BID | ASK | BEST_BID | BEST_ASK.
        interval_minutes: Bar width, 1-1440.
    """
    if not 1 <= interval_minutes <= 1440:
        raise BloombergError("interval_minutes must be between 1 and 1440")
    return await _call(
        "intraday_bars", security, event_type, interval_minutes, start_datetime, end_datetime
    )


@mcp.tool()
async def get_intraday_ticks(
    security: str,
    start_datetime: str,
    end_datetime: str,
    event_types: list[str] | None = None,
) -> dict[str, Any]:
    """Tick-by-tick data for a single security (BLPAPI IntradayTickRequest).

    Keep the window short - ticks are voluminous.

    Args:
        security: A single Bloomberg ticker.
        start_datetime: ISO-8601 UTC.
        end_datetime: ISO-8601 UTC.
        event_types: Any of TRADE, BID, ASK, BID_BEST, ASK_BEST, SETTLE. Defaults to TRADE.
    """
    return await _call(
        "intraday_ticks", security, event_types or ["TRADE"], start_datetime, end_datetime
    )


@mcp.tool()
async def search_instruments(
    query: str, yellow_key: str = "NONE", max_results: int = 20
) -> dict[str, Any]:
    """Resolve a company name, ticker fragment or ISIN to Bloomberg tickers (//blp/instruments).

    Args:
        query: Free text, e.g. "apple", "US0378331005", "vodafone".
        yellow_key: Asset-class filter - NONE, CMDT, EQTY, MUNI, PRFD, CLNT, MMKT, GOVT,
            CORP, INDX, CURR, MTGE.
        max_results: 1-100.
    """
    return await _call("search_instruments", query, yellow_key, max(1, min(max_results, 100)))


@mcp.tool()
async def search_fields(query: str, max_results: int = 25) -> dict[str, Any]:
    """Find Bloomberg field mnemonics by keyword (//blp/apiflds FieldSearchRequest).

    Use this to turn "market cap" into CUR_MKT_CAP before calling get_reference_data.
    """
    return await _call("search_fields", query, max(1, min(max_results, 100)))


@mcp.tool()
async def run_equity_screen(screen_name: str, screen_type: str = "GLOBAL") -> dict[str, Any]:
    """Run a saved Bloomberg EQS equity screen (BLPAPI BeqsRequest).

    Args:
        screen_name: Exact screen name as saved in the terminal.
        screen_type: GLOBAL for Bloomberg-published screens, PRIVATE for your own.
    """
    return await _call("equity_screen", screen_name, screen_type)


@mcp.resource("bloomberg://cheatsheet")
def cheatsheet() -> str:
    """Common Bloomberg yellow keys and field mnemonics."""
    return (
        "# Bloomberg quick reference\n\n"
        "## Yellow keys (security suffix)\n"
        "Equity, Index, Curncy, Comdty, Corp, Govt, Mtge, Muni, Pfd\n"
        "Examples: `IBM US Equity`, `SPX Index`, `EURUSD Curncy`, `CL1 Comdty`, "
        "`USGG10YR Index`\n\n"
        "## Frequently used fields\n"
        "- Price: PX_LAST, PX_OPEN, PX_HIGH, PX_LOW, PX_BID, PX_ASK, PX_VOLUME, VOLUME\n"
        "- Changes: CHG_PCT_1D, CHG_PCT_YTD, VOLATILITY_30D\n"
        "- Reference: NAME, LONG_COMP_NAME, CRNCY, SECURITY_TYP, ID_ISIN, ID_CUSIP, "
        "GICS_SECTOR_NAME, EXCH_CODE\n"
        "- Fundamentals: CUR_MKT_CAP, PE_RATIO, PX_TO_BOOK_RATIO, EQY_DVD_YLD_IND, "
        "SALES_REV_TURN, EBITDA, TOT_DEBT_TO_TOT_ASSET\n"
        "- Fixed income: YLD_YTM_MID, DUR_ADJ_MID, CPN, MATURITY, AMT_OUTSTANDING\n"
        "- Estimates: BEST_EPS, BEST_TARGET_PRICE (with override BEST_FPERIOD_OVERRIDE=1FY)\n"
    )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    try:
        info = await anyio.to_thread.run_sync(lambda: get_backend().info())
        return JSONResponse({"status": "ok", "mcp_path": settings.mcp_path, "backend": info})
    except Exception as exc:  # health must answer even when Bloomberg is down
        return JSONResponse({"status": "degraded", "error": str(exc)}, status_code=503)


class ApiKeyMiddleware:
    """Optional shared secret between the gateway and this server."""

    def __init__(self, app: Any, api_key: str, header: str) -> None:
        self.app = app
        self.api_key = api_key
        self.header = header.lower().encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") == "/healthz":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        supplied = headers.get(self.header)
        if supplied is None:
            auth = headers.get(b"authorization", b"")
            if auth.lower().startswith(b"bearer "):
                supplied = auth[7:]
        if supplied != self.api_key.encode():
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_app() -> Any:
    """Starlette ASGI app serving MCP at settings.mcp_path."""
    transport_security = None
    if settings.allowed_hosts or settings.allowed_origins:
        from mcp.server.transport_security import TransportSecuritySettings

        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.allowed_hosts,
            allowed_origins=settings.allowed_origins,
        )
        log.info("DNS rebinding protection on; allowed hosts: %s", settings.allowed_hosts)

    app: Any = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        stateless_http=settings.stateless,
        json_response=settings.json_response,
        transport_security=transport_security,
    )
    if settings.api_key:
        app = ApiKeyMiddleware(app, settings.api_key, settings.api_key_header)
        log.info("API key enforcement enabled (header: %s)", settings.api_key_header)
    return app
