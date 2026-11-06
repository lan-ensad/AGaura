# syntax=docker/dockerfile:1

# --- Build stage: resolve and install dependencies with uv -------------------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, for layer caching.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the project itself, installed as a regular (non-editable) package. The cache keeps the
# project's wheel keyed on pyproject.toml only: rebuild it, or new templates and static files
# would be missing from the image.
COPY README.md ./
COPY agaura ./agaura
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package agaura

# --- Runtime stage ------------------------------------------------------------
FROM python:3.12-slim

# Pango and fonts for WeasyPrint (code cards and minutes as PDF).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 agaura \
    && mkdir /data && chown agaura:agaura /data

COPY --from=builder --chown=agaura:agaura /app/.venv /app/.venv

# Production unless .env says otherwise: a missing or incomplete .env then stops the start on
# the missing secret key instead of running with the development one.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AGAURA_DATA_DIR=/data \
    AGAURA_ENVIRONMENT=production

USER agaura
WORKDIR /app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# Single process on purpose: real-time fan-out is in-memory (see docs/SPEC.md §12.2).
# No uvicorn access log: Caddy logs requests with the secret path tokens filtered out.
# X-Forwarded-For is trusted only from private networks, where Docker places Caddy (IPv4 pools
# and the IPv6 ULA subnet of compose.yaml): should port 8000 ever be published, a client on the
# Internet could not choose the address that the rate limits count.
CMD ["uvicorn", "agaura.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7", \
     "--no-access-log"]
