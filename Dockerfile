FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# Bloomberg's SDK lives on their own index, not PyPI, and ships no linux/arm64
# wheel. The `||` keeps the build green without it: the server then runs in mock
# mode. Build for linux/amd64 to get the real thing.
RUN pip install --no-cache-dir \
      --index-url https://blpapi.bloomberg.com/repository/releases/python/simple/ \
      blpapi \
 || echo "blpapi unavailable - image will run in mock mode"

# Choreo requires a non-root user with a UID between 10000 and 20000.
RUN groupadd -g 10014 choreo \
 && useradd -u 10014 -g choreo -M -s /usr/sbin/nologin choreouser

ENV MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8000 \
    MCP_PATH=/mcp \
    MCP_STATELESS=true \
    MCP_JSON_RESPONSE=true

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz').read()"

USER 10014
CMD ["python", "-m", "bloomberg_mcp"]
