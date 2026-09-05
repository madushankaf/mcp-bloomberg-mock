# Bloomberg MCP Server

An MCP server that exposes Bloomberg data over **streamable HTTP at `/mcp`**, ready to sit
behind the WSO2 AI Gateway (or any other MCP-aware proxy).

It wraps Bloomberg's own SDK — **BLPAPI**, the Bloomberg Open API — so the same entitlements
and data you get from the Terminal, SAPI or B-PIPE are what the tools return. No scraping,
no unofficial endpoints.

```
MCP client ──▶ WSO2 AI Gateway ──▶ http://bloomberg-mcp:8000/mcp ──▶ BLPAPI ──▶ Bloomberg
```

## Tools

| Tool | BLPAPI request | What it does |
|---|---|---|
| `bloomberg_status` | – | Which backend is live (real vs mock), connection details |
| `get_reference_data` | `ReferenceDataRequest` (`//blp/refdata`) | Current/static fields — the Excel `BDP()` |
| `get_historical_data` | `HistoricalDataRequest` | End-of-period time series — `BDH()` |
| `get_intraday_bars` | `IntradayBarRequest` | Intraday OHLCV bars (~140 days of history) |
| `get_intraday_ticks` | `IntradayTickRequest` | Tick-by-tick trades/quotes |
| `search_instruments` | `instrumentListRequest` (`//blp/instruments`) | Name / ISIN → Bloomberg ticker |
| `search_fields` | `FieldSearchRequest` (`//blp/apiflds`) | Keyword → field mnemonic |
| `run_equity_screen` | `BeqsRequest` | Run a saved EQS screen |

Plus a `bloomberg://cheatsheet` resource listing common yellow keys and field mnemonics.

Streaming subscriptions (`//blp/mktdata`) are intentionally out of scope — MCP tool calls are
request/response, and refdata snapshots cover the same ground.

## Quick start

```bash
uv venv --python 3.13
uv pip install -e .
cp .env.example .env

uv run python -m bloomberg_mcp            # http://0.0.0.0:8000/mcp
uv run python scripts/smoke_client.py     # exercise every tool
curl -s localhost:8000/healthz | jq
```

With no Bloomberg reachable, the server logs a warning and serves **deterministic mock data**
(every payload carries `"mock": true`), so the gateway wiring can be built and tested first.

## Connecting to real Bloomberg

Install Bloomberg's SDK — it lives on Bloomberg's own package index, not PyPI:

```bash
uv pip install blpapi          # the index is preconfigured in pyproject.toml
# or, with plain pip:
pip install --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ blpapi
```

Then pick a transport in `.env`:

**Desktop API (DAPI)** — free with a Terminal licence, one user, Terminal must be running and
logged in on the same machine:

```env
BLOOMBERG_MODE=blpapi
BLOOMBERG_HOST=localhost
BLOOMBERG_PORT=8194
```

**Server API (SAPI) / B-PIPE** — the licensed server-side feeds; this is what you want if the
MCP server runs in a container or in the cloud:

```env
BLOOMBERG_MODE=blpapi
BLOOMBERG_HOST=bpipe-host.internal
BLOOMBERG_PORT=8194
BLOOMBERG_AUTH_OPTIONS=AuthenticationMode=APPLICATION_ONLY;ApplicationAuthenticationType=APPNAME_AND_KEY;ApplicationName=my-app
BLOOMBERG_TLS_CLIENT_CERT=/certs/client.pk12
BLOOMBERG_TLS_CLIENT_CERT_PASSWORD=...
BLOOMBERG_TLS_TRUST_MATERIAL=/certs/rootCertificate.pk7
```

`BLOOMBERG_MODE=blpapi` fails fast if Bloomberg is unreachable — use it in production so a
misconfiguration can't silently serve mock data. `auto` (the default) falls back to mock.

Note that Bloomberg's licence terms govern redistribution of this data; putting it behind a
gateway for multiple consumers is exactly the case B-PIPE/SAPI entitlements exist for. Check
your agreement before opening the gateway route up beyond entitled users.

## Behind the WSO2 AI Gateway

Point the gateway's MCP backend at `http://<host>:8000/mcp` and let it own client-facing
authentication, rate limiting and observability. The defaults are already gateway-friendly:

- `MCP_STATELESS=true` — no server-side session state, so any replica can serve any request
  and you need no session affinity. A single POST is a complete call; no `initialize`
  handshake or `Mcp-Session-Id` to carry:

  ```bash
  curl -X POST http://localhost:8000/mcp \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{
         "name":"get_reference_data",
         "arguments":{"securities":["IBM US Equity"],"fields":["PX_LAST"]}}}'
  ```

- `MCP_JSON_RESPONSE=true` — plain `application/json` responses instead of SSE streams, which
  proxies handle far more predictably.
- `MCP_API_KEY` — optional shared secret between the gateway and this server, accepted as
  `x-api-key: <key>` or `Authorization: Bearer <key>`; anything else gets 401. `/healthz`
  stays open for probes.
- DNS-rebinding protection is off by default, because behind a proxy the `Host` header is the
  gateway's, not yours. Set `MCP_ALLOWED_HOSTS=bloomberg-mcp:8000,localhost:*` to turn it on.

### Container

```bash
docker build -t bloomberg-mcp .
docker run -p 8000:8000 --env-file .env bloomberg-mcp
```

## Deploying on Choreo

The repo carries everything Choreo needs — `.choreo/component.yaml`, `openapi.yaml` and the
`Dockerfile` — following the [docker-rest-user-service](https://github.com/wso2/choreo-samples/tree/main/docker-rest-user-service)
sample layout.

1. Push this directory to a Git repository Choreo can read.
2. Create a **Service** component, buildpack **Docker**, Docker context `/`, Dockerfile
   `/Dockerfile`.
3. Choreo reads `.choreo/component.yaml`: a public REST endpoint on port 8000 with
   `basePath: /`, its resources generated from `openapi.yaml`.
4. Build, then set the environment variables on the Deploy page (all optional — a blank
   field falls back to the image default) and deploy.

The invoke URL ends in `/mcp`, which is what you hand to MCP clients:

```
https://<env>-<org>.<region>.choreoapis.dev/<project>/<component>/<version>/mcp
```

Some things worth knowing before you deploy:

- **Only declared paths are routed.** Choreo generates the managed API's resources from
  `openapi.yaml`, so `POST /mcp` and `GET /healthz` are declared there. Adding a route to the
  server without adding it to the spec gets you a 404 from the gateway.
- **Stateless is what makes this work.** `MCP_STATELESS=true` and `MCP_JSON_RESPONSE=true` are
  baked into the image, so every call is one self-contained POST returning plain JSON — no
  session affinity across replicas, no SSE for the gateway to hold open.
- **The managed API has its own auth.** Choreo protects the endpoint with OAuth2 by default;
  your MCP client needs a token or a Choreo API key. `MCP_API_KEY` is a *second*, optional
  secret checked by this server — useful as defence in depth, not a replacement.
- **blpapi installs on Choreo but not on an Apple Silicon Mac.** Bloomberg publishes no
  linux/arm64 wheel, so a local `docker build` falls back to mock mode; Choreo builds amd64,
  where the real SDK installs.
- **The Desktop API is unreachable from Choreo.** `localhost:8194` inside a container is the
  container. Real data means a SAPI or B-PIPE endpoint Choreo can route to; without one the
  server serves mock data and says so in `bloomberg_status`.
- The container runs as UID 10014, per Choreo's non-root requirement.

## Configuration

Every setting is an environment variable; see `.env.example` for the full annotated list.

| Variable | Default | Purpose |
|---|---|---|
| `BLOOMBERG_MODE` | `auto` | `auto` / `blpapi` / `mock` |
| `BLOOMBERG_HOST` / `BLOOMBERG_PORT` | `localhost` / `8194` | BLPAPI endpoint |
| `BLOOMBERG_AUTH_OPTIONS` | – | B-PIPE/SAPI auth string |
| `BLOOMBERG_TLS_*` | – | B-PIPE certificates |
| `BLOOMBERG_TIMEOUT_MS` | `30000` | Per-request timeout |
| `BLOOMBERG_MAX_SECURITIES` / `_MAX_FIELDS` | `100` / `50` | Request guardrails |
| `MCP_HTTP_HOST` / `MCP_HTTP_PORT` | `0.0.0.0` / `8000` | Listen address |
| `MCP_PATH` | `/mcp` | MCP endpoint path |
| `MCP_STATELESS` / `MCP_JSON_RESPONSE` | `true` / `true` | Proxy-friendly transport |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | – | Enable DNS-rebinding protection |
| `MCP_API_KEY` / `MCP_API_KEY_HEADER` | – / `x-api-key` | Backend auth |

## Local client (stdio)

For testing in Claude Code / Claude Desktop without the HTTP hop:

```bash
uv run python -m bloomberg_mcp --transport stdio
```

## Layout

```
src/bloomberg_mcp/
  config.py           settings from environment
  backend.py          backend protocol + BloombergError
  blpapi_backend.py   real BLPAPI session, request plumbing, response parsing
  mock_backend.py     deterministic synthetic data
  server.py           FastMCP tools, /healthz, API-key middleware, ASGI app
  __main__.py         CLI entry point
scripts/smoke_client.py     exercises every tool over HTTP
.choreo/component.yaml      Choreo endpoint + config-form definition
openapi.yaml                API contract Choreo generates its routes from
Dockerfile
```

Built on `mcp` 2.x (`MCPServer`, formerly `FastMCP`).
