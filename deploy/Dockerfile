FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src:/app/lib/trading_platform/src

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --shell /bin/bash agent

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY lib/ /app/lib/
COPY src/ /app/src/
COPY scripts/ /app/scripts/
COPY config/ /app/config/

RUN pip install --upgrade pip setuptools wheel \
    && pip install -e /app/lib/trading_platform \
    && pip install -e /app

RUN mkdir -p /app/data /app/logs && chown -R agent:agent /app

USER agent

# Override per service in docker-compose.yml
CMD ["python", "-m", "orchestrator.main"]
