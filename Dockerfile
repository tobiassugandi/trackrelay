# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.9.17 AS uv

FROM python:3.12.12-slim-bookworm AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

COPY --from=uv /uv /bin/uv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12.12-slim-bookworm AS runtime-base

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACKRELAY_DEBUG=false \
    TRACKRELAY_ENVIRONMENT=production

WORKDIR /app

RUN groupadd --gid 10001 trackrelay \
    && useradd \
        --uid 10001 \
        --gid trackrelay \
        --home-dir /nonexistent \
        --no-create-home \
        --shell /usr/sbin/nologin \
        trackrelay

COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001

STOPSIGNAL SIGTERM

FROM runtime-base AS worker

CMD ["trackrelay-worker"]

FROM runtime-base AS simulator

EXPOSE 8001

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "trackrelay.healthcheck", "8001"]

CMD ["uvicorn", "trackrelay.downstream.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

FROM runtime-base AS api

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-m", "trackrelay.healthcheck", "8000"]

CMD ["uvicorn", "trackrelay.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
