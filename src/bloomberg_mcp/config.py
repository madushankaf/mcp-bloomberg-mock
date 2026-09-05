"""Runtime configuration, all overridable by environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw is not None else default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- backend selection -------------------------------------------------
    # auto   : use blpapi if importable and a session starts, else mock
    # blpapi : require a real Bloomberg connection (fail fast otherwise)
    # mock   : deterministic fake data, no Bloomberg needed
    mode: str = field(default_factory=lambda: (_env("BLOOMBERG_MODE", "auto") or "auto").lower())

    # --- Bloomberg connection ---------------------------------------------
    # localhost:8194 is the Desktop API (Terminal must be running and logged in).
    # Point at a SAPI / B-PIPE host otherwise.
    bbg_host: str = field(default_factory=lambda: _env("BLOOMBERG_HOST", "localhost"))
    bbg_port: int = field(default_factory=lambda: _env_int("BLOOMBERG_PORT", 8194))
    # B-PIPE / SAPI only. e.g. "AuthenticationType=OS_LOGON" or
    # "AuthenticationMode=APPLICATION_ONLY;ApplicationAuthenticationType=APPNAME_AND_KEY;ApplicationName=myapp"
    auth_options: str | None = field(default_factory=lambda: _env("BLOOMBERG_AUTH_OPTIONS"))
    tls_client_cert: str | None = field(default_factory=lambda: _env("BLOOMBERG_TLS_CLIENT_CERT"))
    tls_client_cert_password: str | None = field(
        default_factory=lambda: _env("BLOOMBERG_TLS_CLIENT_CERT_PASSWORD")
    )
    tls_trust_material: str | None = field(default_factory=lambda: _env("BLOOMBERG_TLS_TRUST_MATERIAL"))
    request_timeout_ms: int = field(default_factory=lambda: _env_int("BLOOMBERG_TIMEOUT_MS", 30_000))

    # --- guardrails --------------------------------------------------------
    max_securities: int = field(default_factory=lambda: _env_int("BLOOMBERG_MAX_SECURITIES", 100))
    max_fields: int = field(default_factory=lambda: _env_int("BLOOMBERG_MAX_FIELDS", 50))

    # --- HTTP / MCP transport ---------------------------------------------
    http_host: str = field(default_factory=lambda: _env("MCP_HTTP_HOST", "0.0.0.0"))
    http_port: int = field(default_factory=lambda: _env_int("MCP_HTTP_PORT", 8000))
    mcp_path: str = field(default_factory=lambda: _env("MCP_PATH", "/mcp"))
    # Stateless = no server-side session state, every POST is self-contained.
    # Strongly recommended behind a gateway / load balancer such as WSO2.
    stateless: bool = field(default_factory=lambda: _env_bool("MCP_STATELESS", True))
    # Plain application/json responses instead of SSE streams. Easier to proxy.
    json_response: bool = field(default_factory=lambda: _env_bool("MCP_JSON_RESPONSE", True))
    # DNS-rebinding protection. Off unless you list hosts; behind a gateway the
    # Host header is the gateway's, so leave empty or list the internal name.
    allowed_hosts: list[str] = field(
        default_factory=lambda: [h.strip() for h in (_env("MCP_ALLOWED_HOSTS", "") or "").split(",") if h.strip()]
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: [o.strip() for o in (_env("MCP_ALLOWED_ORIGINS", "") or "").split(",") if o.strip()]
    )
    # Optional shared secret between the gateway and this server.
    api_key: str | None = field(default_factory=lambda: _env("MCP_API_KEY"))
    api_key_header: str = field(default_factory=lambda: _env("MCP_API_KEY_HEADER", "x-api-key"))
    log_level: str = field(default_factory=lambda: (_env("LOG_LEVEL", "info") or "info").lower())


settings = Settings()
