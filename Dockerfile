FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY core ./core
COPY apps ./apps
COPY rules ./rules

RUN pip install --no-cache-dir ".[postgres]"

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8210
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s \
  CMD curl -fsS http://127.0.0.1:8210/healthz || exit 1

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8210"]
