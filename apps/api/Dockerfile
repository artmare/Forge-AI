FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

FROM base AS dependencies
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt

FROM dependencies AS lint
COPY . .
RUN ruff check .

FROM dependencies AS test
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY . .
CMD ["pytest", "-q"]

FROM base AS runtime
RUN addgroup --system --gid 10001 forge \
    && adduser --system --uid 10001 --ingroup forge forge
RUN mkdir -p /workspaces && chown forge:forge /workspaces
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=forge:forge app ./app
COPY --chown=forge:forge alembic ./alembic
COPY --chown=forge:forge alembic.ini ./alembic.ini
USER forge
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
