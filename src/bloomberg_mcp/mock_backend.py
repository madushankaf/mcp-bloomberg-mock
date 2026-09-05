"""Deterministic fake Bloomberg data.

Lets you stand the MCP server up and wire it into WSO2 before a Terminal,
SAPI or B-PIPE entitlement is available. Every payload carries "mock": true so
downstream consumers can never mistake it for real market data.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from typing import Any

from .backend import BloombergError
from .config import Settings

_NAMES = {
    "IBM US EQUITY": "INTL BUSINESS MACHINES CORP",
    "AAPL US EQUITY": "APPLE INC",
    "MSFT US EQUITY": "MICROSOFT CORP",
    "USDLKR CURNCY": "USD-LKR X-RATE",
    "SPX INDEX": "S&P 500 INDEX",
}

_FIELD_META = {
    "PX_LAST": ("Last price", "Double"),
    "PX_OPEN": ("Open price", "Double"),
    "PX_HIGH": ("High price", "Double"),
    "PX_LOW": ("Low price", "Double"),
    "PX_BID": ("Bid price", "Double"),
    "PX_ASK": ("Ask price", "Double"),
    "VOLUME": ("Volume", "Double"),
    "NAME": ("Name", "String"),
    "CRNCY": ("Currency", "String"),
    "CUR_MKT_CAP": ("Current market cap", "Double"),
    "CHG_PCT_1D": ("1 day percent change", "Double"),
    "SECURITY_TYP": ("Security type", "String"),
}


def _seed(*parts: str) -> int:
    digest = hashlib.sha256("|".join(parts).upper().encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _unit(*parts: str) -> float:
    """Stable pseudo-random float in [0, 1) for the given key."""
    return (_seed(*parts) % 1_000_000) / 1_000_000


def _base_price(security: str) -> float:
    return round(10 + _unit(security, "base") * 490, 2)


def _value_for(security: str, field: str) -> Any:
    field = field.upper()
    price = _base_price(security)
    if field == "NAME":
        return _NAMES.get(security.upper(), f"{security.split()[0].upper()} MOCK ENTITY")
    if field == "CRNCY":
        return "USD"
    if field == "SECURITY_TYP":
        return "Common Stock"
    if field == "PX_LAST":
        return price
    if field == "PX_OPEN":
        return round(price * (1 + (_unit(security, "open") - 0.5) * 0.02), 2)
    if field == "PX_HIGH":
        return round(price * (1 + _unit(security, "high") * 0.02), 2)
    if field == "PX_LOW":
        return round(price * (1 - _unit(security, "low") * 0.02), 2)
    if field == "PX_BID":
        return round(price * 0.999, 2)
    if field == "PX_ASK":
        return round(price * 1.001, 2)
    if field == "VOLUME":
        return float(int(1e5 + _unit(security, "vol") * 5e7))
    if field == "CUR_MKT_CAP":
        return round(price * (1e7 + _unit(security, "mcap") * 5e9), 2)
    if field == "CHG_PCT_1D":
        return round((_unit(security, "chg") - 0.5) * 6, 4)
    return None


class MockBackend:
    name = "mock"

    def __init__(self, settings: Settings, reason: str | None = None) -> None:
        self._settings = settings
        self.reason = reason

    def info(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "mock": True,
            "connected": True,
            "reason": self.reason or "BLOOMBERG_MODE=mock",
            "note": "Synthetic data. Set BLOOMBERG_MODE=blpapi with a reachable "
            "Terminal/SAPI/B-PIPE endpoint for real data.",
        }

    def reference_data(
        self,
        securities: list[str],
        fields: list[str],
        overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        rows = []
        for security in securities:
            values: dict[str, Any] = {}
            exceptions = []
            for field in fields:
                value = _value_for(security, field)
                if value is None:
                    exceptions.append({"field": field, "message": "Unknown field (mock backend)"})
                else:
                    values[field.upper()] = value
            rows.append(
                {"security": security, "fields": values, "fieldExceptions": exceptions, "error": None}
            )
        return {"mock": True, "securities": rows}

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
    ) -> dict[str, Any]:
        start = _as_date(start_date)
        end = _as_date(end_date)
        if end < start:
            raise BloombergError("end_date must not be before start_date")
        step = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30, "QUARTERLY": 91, "YEARLY": 365}.get(
            periodicity.upper(), 1
        )

        series = []
        for security in securities:
            base = _base_price(security)
            points = []
            day = start
            index = 0
            while day <= end:
                if step > 1 or day.weekday() < 5:
                    drift = math.sin(index / 9 + _unit(security, "phase") * 6.28)
                    noise = (_unit(security, day.isoformat()) - 0.5) * 0.04
                    factor = 1 + drift * 0.08 + noise
                    row: dict[str, Any] = {"date": day.isoformat()}
                    for field in fields:
                        name = field.upper()
                        if name == "VOLUME":
                            row[name] = float(int(1e5 + _unit(security, day.isoformat(), "v") * 5e7))
                        else:
                            row[name] = round(base * factor, 2)
                    points.append(row)
                    index += 1
                    if max_data_points and len(points) >= max_data_points:
                        break
                day += dt.timedelta(days=step if step > 1 else 1)
            series.append({"security": security, "data": points, "fieldExceptions": [], "error": None})
        return {"mock": True, "series": series}

    def intraday_bars(
        self,
        security: str,
        event_type: str,
        interval_minutes: int,
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]:
        start = _as_datetime(start_datetime)
        end = _as_datetime(end_datetime)
        base = _base_price(security)
        bars = []
        cursor = start
        index = 0
        while cursor < end and len(bars) < 500:
            factor = 1 + math.sin(index / 7 + _unit(security, "ib") * 6.28) * 0.01
            open_px = round(base * factor, 2)
            close_px = round(open_px * (1 + (_unit(security, cursor.isoformat()) - 0.5) * 0.006), 2)
            bars.append(
                {
                    "time": cursor.isoformat(),
                    "open": open_px,
                    "high": round(max(open_px, close_px) * 1.002, 2),
                    "low": round(min(open_px, close_px) * 0.998, 2),
                    "close": close_px,
                    "volume": int(1000 + _unit(security, cursor.isoformat(), "v") * 90000),
                    "numEvents": int(10 + _unit(security, cursor.isoformat(), "n") * 400),
                }
            )
            cursor += dt.timedelta(minutes=interval_minutes)
            index += 1
        return {
            "mock": True,
            "security": security,
            "eventType": event_type.upper(),
            "intervalMinutes": interval_minutes,
            "bars": bars,
        }

    def intraday_ticks(
        self,
        security: str,
        event_types: list[str],
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]:
        start = _as_datetime(start_datetime)
        end = _as_datetime(end_datetime)
        base = _base_price(security)
        ticks = []
        cursor = start
        index = 0
        while cursor < end and len(ticks) < 500:
            event_type = event_types[index % len(event_types)].upper()
            ticks.append(
                {
                    "time": cursor.isoformat(),
                    "type": event_type,
                    "value": round(base * (1 + (_unit(security, cursor.isoformat()) - 0.5) * 0.004), 4),
                    "size": int(1 + _unit(security, cursor.isoformat(), "s") * 5000),
                }
            )
            cursor += dt.timedelta(seconds=30)
            index += 1
        return {"mock": True, "security": security, "ticks": ticks}

    def search_instruments(
        self, query: str, yellow_key: str = "NONE", max_results: int = 20
    ) -> dict[str, Any]:
        suffix = {
            "NONE": "Equity",
            "EQTY": "Equity",
            "CORP": "Corp",
            "GOVT": "Govt",
            "CURNCY": "Curncy",
            "INDEX": "Index",
            "MTGE": "Mtge",
            "MUNI": "Muni",
            "PRFD": "Pfd",
            "CMDT": "Comdty",
        }.get(yellow_key.upper(), "Equity")
        ticker = query.strip().upper().split()[0] or "MOCK"
        exchanges = ["US", "LN", "JP", "GR", "HK", "AU"]
        results = [
            {
                "security": f"{ticker} {exchange} {suffix}",
                "description": f"{_NAMES.get(f'{ticker} {exchange} EQUITY', ticker + ' MOCK ENTITY')}"
                f" ({exchange})",
            }
            for exchange in exchanges[: max(1, min(max_results, len(exchanges)))]
        ]
        return {"mock": True, "query": query, "results": results}

    def search_fields(self, query: str, max_results: int = 25) -> dict[str, Any]:
        needle = query.strip().upper()
        matches = [
            {
                "id": f"MK{index:04d}",
                "mnemonic": mnemonic,
                "description": description,
                "datatype": datatype,
                "categoryName": "Mock",
            }
            for index, (mnemonic, (description, datatype)) in enumerate(_FIELD_META.items())
            if needle in mnemonic or needle in description.upper()
        ]
        return {"mock": True, "query": query, "fields": matches[:max_results]}

    def equity_screen(self, screen_name: str, screen_type: str = "GLOBAL") -> dict[str, Any]:
        tickers = ["AAPL US Equity", "MSFT US Equity", "IBM US Equity", "NVDA US Equity"]
        return {
            "mock": True,
            "screenName": screen_name,
            "screenType": screen_type.upper(),
            "securities": [
                {
                    "security": ticker,
                    "fields": {
                        "PX_LAST": _value_for(ticker, "PX_LAST"),
                        "CUR_MKT_CAP": _value_for(ticker, "CUR_MKT_CAP"),
                    },
                }
                for ticker in tickers
            ],
        }

    def close(self) -> None:  # nothing to release
        return None


def _as_date(value: str) -> dt.date:
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise BloombergError(f"Invalid date {value!r}; expected YYYY-MM-DD")


def _as_datetime(value: str) -> dt.datetime:
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BloombergError(
            f"Invalid datetime {value!r}; expected ISO-8601 e.g. 2024-01-15T13:30:00"
        ) from exc
    return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
