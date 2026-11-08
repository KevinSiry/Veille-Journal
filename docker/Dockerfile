# syntax=docker/dockerfile:1.7
# Veille Journal image. Build from the repository root:
#   docker build -f docker/Dockerfile -t veille-journal:dev .
# Exclusions come from docker/Dockerfile.dockerignore (BuildKit).

ARG PYTHON_VERSION=3.12

# --------------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION}-slim AS builder

# uv binary only (Docker Hub mirror of ghcr.io/astral-sh/uv).
COPY --from=docker.io/astral/uv:0.11.32 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src

# 1) dependencies only: cached as long as pyproject.toml / uv.lock do not change
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable

# 2) the project itself (installed as a regular wheel, not editable)
COPY README.md ./
COPY src ./src
# The check below fails the build if the SQL migrations are missing from the wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && /app/.venv/bin/python -c "import importlib.resources as r; assert r.files('veille.db').joinpath('migrations/001_init.sql').is_file(), 'migrations missing from the wheel'"

# --------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG VERSION=dev
ARG REVISION=unknown

LABEL org.opencontainers.image.title="Veille Journal" \
      org.opencontainers.image.description="Personal nightly news digest (RSS + SearXNG, local Ollama summaries in French)" \
      org.opencontainers.image.source="https://github.com/KevinSiry/Veille-Journal" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

RUN groupadd --system --gid 10001 veille \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --no-create-home \
       --shell /usr/sbin/nologin veille \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

ENV VERSION=${VERSION} \
    PATH=/app/.venv/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATABASE_PATH=/data/veille.db

WORKDIR /app
USER 10001:10001
VOLUME ["/data"]
EXPOSE 8080

# The worker service disables this healthcheck in compose (it serves no HTTP).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"]

CMD ["veille", "serve"]
