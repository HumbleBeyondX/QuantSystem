# ── Python Backend ──────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# System dependencies for asyncpg, scientific libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Dependencies ────────────────────────────────────────────
FROM base AS deps

COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install ".[all]" 2>/dev/null || pip install .

# ── Application ─────────────────────────────────────────────
FROM deps AS app

COPY core/ core/
COPY data/ data/
COPY strategies/ strategies/
COPY portfolio/ portfolio/
COPY execution/ execution/
COPY backtesting/ backtesting/
COPY research/ research/
COPY dashboard/backend/ dashboard/backend/
COPY dashboard/alerts/ dashboard/alerts/
COPY config/ config/
COPY scripts/ scripts/

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/system/health || exit 1

CMD ["uvicorn", "dashboard.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
