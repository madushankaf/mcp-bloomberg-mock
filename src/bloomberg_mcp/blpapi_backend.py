"""Real Bloomberg backend, built on Bloomberg's own BLPAPI SDK.

Covers the request/response services of the Open API:
  //blp/refdata     ReferenceDataRequest, HistoricalDataRequest,
                    IntradayBarRequest, IntradayTickRequest, BeqsRequest
  //blp/instruments instrumentListRequest  (security lookup)
  //blp/apiflds     FieldSearchRequest     (field mnemonic lookup)

Streaming (//blp/mktdata subscriptions) is deliberately out of scope: MCP tool
calls are request/response, and refdata snapshots cover the same ground.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from typing import Any

from .backend import BloombergError
from .config import Settings

log = logging.getLogger(__name__)

REFDATA_SVC = "//blp/refdata"
INSTRUMENTS_SVC = "//blp/instruments"
APIFLDS_SVC = "//blp/apiflds"
AUTH_SVC = "//blp/apiauth"


def _scalar(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _convert(element: Any) -> Any:
    """Recursively turn a blpapi Element into plain JSON-able Python."""
    import blpapi

    datatype = element.datatype()
    is_complex = datatype in (blpapi.DataType.SEQUENCE, blpapi.DataType.CHOICE)

    if element.isArray():
        if is_complex:
            return [_convert(element.getValueAsElement(i)) for i in range(element.numValues())]
        return [_scalar(element.getValue(i)) for i in range(element.numValues())]

    if is_complex:
        return {
            str(element.getElement(i).name()): _convert(element.getElement(i))
            for i in range(element.numElements())
        }

    if element.isNull():
        return None
    return _scalar(element.getValue())


def _parse_datetime(value: str) -> dt.datetime:
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:  # pragma: no cover - user input
        raise BloombergError(
            f"Invalid datetime {value!r}; expected ISO-8601 e.g. 2024-01-15T13:30:00"
        ) from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_date(value: str) -> str:
    """Accept YYYY-MM-DD or YYYYMMDD, return Bloomberg's YYYYMMDD."""
    raw = value.strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return dt.datetime.strptime(raw, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise BloombergError(f"Invalid date {value!r}; expected YYYY-MM-DD")


class BlpapiBackend:
    """Thread-safe wrapper around a single blpapi Session.

    One process holds one session; requests are serialised with a lock so that
    a caller never consumes another caller's events off the session queue.
    """

    name = "blpapi"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._session: Any = None
        self._identity: Any = None
        self._opened: set[str] = set()

    # ------------------------------------------------------------------ session

    def _ensure_session(self) -> Any:
        with self._lock:
            if self._session is not None:
                return self._session
            try:
                import blpapi
            except ImportError as exc:  # pragma: no cover - env dependent
                raise BloombergError(
                    "blpapi is not installed. Install Bloomberg's SDK:\n"
                    "  uv pip install --index-url "
                    "https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi"
                ) from exc

            s = self._settings
            options = blpapi.SessionOptions()
            options.setServerHost(s.bbg_host)
            options.setServerPort(s.bbg_port)
            options.setAutoRestartOnDisconnection(True)
            if s.auth_options:
                options.setAuthenticationOptions(s.auth_options)
            if s.tls_client_cert and s.tls_trust_material:
                options.setTlsOptions(
                    blpapi.TlsOptions.createFromFiles(
                        s.tls_client_cert,
                        s.tls_client_cert_password or "",
                        s.tls_trust_material,
                    )
                )

            session = blpapi.Session(options)
            if not session.start():
                raise BloombergError(
                    f"Could not start a Bloomberg session against {s.bbg_host}:{s.bbg_port}. "
                    "For the Desktop API the Terminal must be running and logged in."
                )
            self._session = session
            if s.auth_options:
                self._identity = self._authorize(session)
            log.info("Bloomberg session started (%s:%s)", s.bbg_host, s.bbg_port)
            return session

    def _authorize(self, session: Any) -> Any:
        """B-PIPE / SAPI: token generation + identity authorization."""
        import blpapi

        timeout_ms = self._settings.request_timeout_ms
        token_cid = blpapi.CorrelationId()
        session.generateToken(correlationId=token_cid)
        token = None
        deadline = time.monotonic() + timeout_ms / 1000
        while token is None:
            if time.monotonic() > deadline:
                raise BloombergError("Timed out waiting for a Bloomberg auth token")
            event = session.nextEvent(1000)
            for msg in event:
                if str(msg.messageType()) == "TokenGenerationSuccess":
                    token = msg.getElementAsString("token")
                elif str(msg.messageType()) == "TokenGenerationFailure":
                    raise BloombergError(f"Token generation failed: {_convert(msg.asElement())}")

        self._open_service(session, AUTH_SVC)
        auth_service = session.getService(AUTH_SVC)
        request = auth_service.createAuthorizationRequest()
        request.set("token", token)
        identity = session.createIdentity()
        session.sendAuthorizationRequest(request, identity, blpapi.CorrelationId())

        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if time.monotonic() > deadline:
                raise BloombergError("Timed out waiting for Bloomberg authorization")
            event = session.nextEvent(1000)
            for msg in event:
                mtype = str(msg.messageType())
                if mtype == "AuthorizationSuccess":
                    return identity
                if mtype == "AuthorizationFailure":
                    raise BloombergError(f"Authorization failed: {_convert(msg.asElement())}")

    def _open_service(self, session: Any, service: str) -> None:
        if service in self._opened:
            return
        if not session.openService(service):
            raise BloombergError(f"Could not open Bloomberg service {service}")
        self._opened.add(service)

    def _service(self, name: str) -> Any:
        session = self._ensure_session()
        self._open_service(session, name)
        return session.getService(name)

    # ------------------------------------------------------------------ plumbing

    def _send(self, request: Any) -> list[dict[str, Any]]:
        """Send a request and collect every message of its response."""
        import blpapi

        session = self._ensure_session()
        cid = blpapi.CorrelationId()
        timeout_s = self._settings.request_timeout_ms / 1000
        messages: list[dict[str, Any]] = []

        with self._lock:
            session.sendRequest(request, identity=self._identity, correlationId=cid)
            deadline = time.monotonic() + timeout_s
            while True:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise BloombergError(f"Bloomberg request timed out after {timeout_s:.0f}s")
                event = session.nextEvent(remaining_ms)
                event_type = event.eventType()
                if event_type == blpapi.Event.TIMEOUT:
                    raise BloombergError(f"Bloomberg request timed out after {timeout_s:.0f}s")
                if event_type in (blpapi.Event.PARTIAL_RESPONSE, blpapi.Event.RESPONSE):
                    for msg in event:
                        if cid not in list(msg.correlationIds()):
                            continue
                        payload = _convert(msg.asElement())
                        if isinstance(payload, dict) and "responseError" in payload:
                            raise BloombergError(f"Bloomberg error: {payload['responseError']}")
                        messages.append(payload if isinstance(payload, dict) else {"value": payload})
                    if event_type == blpapi.Event.RESPONSE:
                        break
                elif event_type == blpapi.Event.SESSION_STATUS:
                    for msg in event:
                        if str(msg.messageType()) in ("SessionTerminated", "SessionStartupFailure"):
                            self._session = None
                            self._opened.clear()
                            raise BloombergError(f"Bloomberg session lost: {_convert(msg.asElement())}")
        return messages

    # ------------------------------------------------------------------ tools

    def info(self) -> dict[str, Any]:
        s = self._settings
        connected = self._session is not None
        return {
            "backend": self.name,
            "host": s.bbg_host,
            "port": s.bbg_port,
            "connected": connected,
            "authenticated": self._identity is not None,
            "services_open": sorted(self._opened),
        }

    def reference_data(
        self,
        securities: list[str],
        fields: list[str],
        overrides: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = self._service(REFDATA_SVC).createRequest("ReferenceDataRequest")
        for security in securities:
            request.getElement("securities").appendValue(security)
        for fld in fields:
            request.getElement("fields").appendValue(fld)
        if overrides:
            element = request.getElement("overrides")
            for key, value in overrides.items():
                override = element.appendElement()
                override.setElement("fieldId", key)
                override.setElement("value", str(value))

        rows: list[dict[str, Any]] = []
        for msg in self._send(request):
            for entry in msg.get("securityData", []) or []:
                rows.append(
                    {
                        "security": entry.get("security"),
                        "fields": entry.get("fieldData") or {},
                        "fieldExceptions": _field_exceptions(entry),
                        "error": (entry.get("securityError") or {}).get("message")
                        if entry.get("securityError")
                        else None,
                    }
                )
        return {"securities": rows}

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
        request = self._service(REFDATA_SVC).createRequest("HistoricalDataRequest")
        for security in securities:
            request.getElement("securities").appendValue(security)
        for fld in fields:
            request.getElement("fields").appendValue(fld)
        request.set("startDate", _parse_date(start_date))
        request.set("endDate", _parse_date(end_date))
        request.set("periodicitySelection", periodicity.upper())
        request.set("periodicityAdjustment", "CALENDAR")
        if currency:
            request.set("currency", currency.upper())
        if adjust_split:
            request.set("adjustmentSplit", True)
        if max_data_points:
            request.set("maxDataPoints", int(max_data_points))

        series: list[dict[str, Any]] = []
        for msg in self._send(request):
            entry = msg.get("securityData") or {}
            series.append(
                {
                    "security": entry.get("security"),
                    "data": entry.get("fieldData") or [],
                    "fieldExceptions": _field_exceptions(entry),
                    "error": (entry.get("securityError") or {}).get("message")
                    if entry.get("securityError")
                    else None,
                }
            )
        return {"series": series}

    def intraday_bars(
        self,
        security: str,
        event_type: str,
        interval_minutes: int,
        start_datetime: str,
        end_datetime: str,
    ) -> dict[str, Any]:
        request = self._service(REFDATA_SVC).createRequest("IntradayBarRequest")
        request.set("security", security)
        request.set("eventType", event_type.upper())
        request.set("interval", int(interval_minutes))
        request.set("startDateTime", _parse_datetime(start_datetime))
        request.set("endDateTime", _parse_datetime(end_datetime))

        bars: list[dict[str, Any]] = []
        for msg in self._send(request):
            bars.extend((msg.get("barData") or {}).get("barTickData") or [])
        return {
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
        request = self._service(REFDATA_SVC).createRequest("IntradayTickRequest")
        request.set("security", security)
        for event_type in event_types:
            request.getElement("eventTypes").appendValue(event_type.upper())
        request.set("startDateTime", _parse_datetime(start_datetime))
        request.set("endDateTime", _parse_datetime(end_datetime))
        request.set("includeConditionCodes", True)

        ticks: list[dict[str, Any]] = []
        for msg in self._send(request):
            ticks.extend((msg.get("tickData") or {}).get("tickData") or [])
        return {"security": security, "ticks": ticks}

    def search_instruments(
        self, query: str, yellow_key: str = "NONE", max_results: int = 20
    ) -> dict[str, Any]:
        request = self._service(INSTRUMENTS_SVC).createRequest("instrumentListRequest")
        request.set("query", query)
        request.set("yellowKeyFilter", f"YK_FILTER_{yellow_key.upper()}")
        request.set("maxResults", int(max_results))

        results: list[dict[str, Any]] = []
        for msg in self._send(request):
            results.extend(msg.get("results") or [])
        return {"query": query, "results": results}

    def search_fields(self, query: str, max_results: int = 25) -> dict[str, Any]:
        request = self._service(APIFLDS_SVC).createRequest("FieldSearchRequest")
        request.set("searchSpec", query)
        request.getElement("returnFieldDocumentation").setValue(False)

        fields: list[dict[str, Any]] = []
        for msg in self._send(request):
            for entry in msg.get("fieldData") or []:
                info = entry.get("fieldInfo") or {}
                fields.append(
                    {
                        "id": entry.get("id"),
                        "mnemonic": info.get("mnemonic"),
                        "description": info.get("description"),
                        "datatype": info.get("datatype"),
                        "categoryName": info.get("categoryName"),
                    }
                )
        return {"query": query, "fields": fields[:max_results]}

    def equity_screen(self, screen_name: str, screen_type: str = "GLOBAL") -> dict[str, Any]:
        request = self._service(REFDATA_SVC).createRequest("BeqsRequest")
        request.set("screenName", screen_name)
        request.set("screenType", screen_type.upper())

        rows: list[dict[str, Any]] = []
        for msg in self._send(request):
            for entry in (msg.get("data") or {}).get("securityData") or []:
                rows.append(
                    {"security": entry.get("security"), "fields": entry.get("fieldData") or {}}
                )
        return {"screenName": screen_name, "screenType": screen_type.upper(), "securities": rows}

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                try:
                    self._session.stop()
                finally:
                    self._session = None
                    self._identity = None
                    self._opened.clear()


def _field_exceptions(entry: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for exception in entry.get("fieldExceptions") or []:
        info = exception.get("errorInfo") or {}
        out.append({"field": exception.get("fieldId"), "message": info.get("message")})
    return out
