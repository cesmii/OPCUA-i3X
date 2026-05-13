# i3xua production container.
#
# Runtime mounts the operator must provide:
#   -v $(pwd)/config.yaml:/config/config.yaml:ro          # required
#   -v $(pwd)/certs:/certs:ro                             # only when channel.mode != None
#                                                        # (cert + key paths in config must
#                                                        # then point under /certs/)
#
# Network: the wrapper needs reachability to its OPC UA server(s). For local
# development, run with `--network=host` to reach `opc.tcp://localhost:62541`.
# In production, wire normal docker / k8s networking — the wrapper just opens
# outbound TCP to whatever `connections[].endpoint` resolves to.

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --home-dir /home/app --create-home app \
 && chown -R app:app /app /home/app
USER app

# uv needs a writable cache dir; `--no-dev --frozen` skips network/resolve work.
ENV UV_CACHE_DIR=/home/app/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/app/.venv

EXPOSE 8080

# Healthcheck hits /healthz on the wrapper. Slim image has no curl, so use
# stdlib urllib. /healthz returns 200 once startup completes; bearer-token
# auth (when enabled) doesn't gate /healthz so no token is needed here.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)" \
  || exit 1

ENTRYPOINT ["uv", "run", "--no-dev", "--frozen", "i3xua"]
CMD ["--config", "/config/config.yaml"]
