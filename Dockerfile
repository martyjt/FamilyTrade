FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.14.3-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "familytrade.api:app", "--host", "0.0.0.0", "--port", "8000"]
