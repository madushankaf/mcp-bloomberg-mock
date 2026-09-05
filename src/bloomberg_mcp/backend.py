"""Backend protocol shared by the real BLPAPI client and the mock."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError


class BloombergError(ToolError, RuntimeError):
    """Raised for connection failures and Bloomberg-reported request errors.

    Subclasses the SDK's ToolError so the message reaches the calling model
    instead of being masked as an unexpected crash.
    """


class Backend(Protocol):
    name: str

    def info(self) -> dict[str, Any]: ...

    def reference_data(
        self,
        securities: list[str],
        fields: list[str],
        overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def historical_data(
        self,
        securities: list[str],
        fields: list[str],
        start_date: str,
        end_date: str,
        periodicity: str = "DAILY",
        currency: str | None = None,
        adjust_split: bool = True,
        max_data_points: int | None = None,
    ) -> dict[str, Any]: ...

    def intraday_bars(
        self,
        security: str,
        event_type: str,
        interval_minutes: int,
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]: ...

    def intraday_ticks(
        self,
        security: str,
        event_types: list[str],
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]: ...

    def search_instruments(
        self, query: str, yellow_key: str = "NONE", max_results: int = 20
    ) -> dict[str, Any]: ...

    def search_fields(self, query: str, max_results: int = 25) -> dict[str, Any]: ...

    def equity_screen(self, screen_name: str, screen_type: str = "GLOBAL") -> dict[str, Any]: ...

    def close(self) -> None: ...
