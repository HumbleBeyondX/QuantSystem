# ============================================================
# QuantSystem Live Trading — Production Docker Image
# ============================================================
# Lean image: only live runner + dashboard API, no ML/RL/DB
# Target: AWS/DigitalOcean/Vultr VPS (Tokyo/Singapore/HK)
#
# Build:  docker build -t quantsystem-live -f deploy/Dockerfile .
# Run:    docker run --env-file .env quantsystem-live
# ============================================================

FROM python:3.13-slim AS base

# Prevent Python from writing .pyc and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

WORKDIR /app

# ── System deps ──
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        tini && \
    rm -rf /var/lib/apt/lists/*

# ── Python deps (cached layer) ──
COPY deploy/requirements-live.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && \
    rm /tmp/requirements.txt

# ── Application code ──
COPY core/ /app/core/
COPY strategies/ /app/strategies/
COPY execution/ /app/execution/
COPY portfolio/ /app/portfolio/
COPY backtesting/ /app/backtesting/
COPY config/ /app/config/
COPY dashboard/backend/ /app/dashboard/backend/
COPY dashboard/alerts/ /app/dashboard/alerts/

# Create empty __init__.py files where needed
RUN touch /app/__init__.py

# ── Health check ──
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://127.0.0.1:8002/health || exit 1

# ── Runtime ──
EXPOSE 8002

# Use tini as init system for proper signal handling
ENTRYPOINT ["tini", "--"]

# Default: run ETH sniper live trading
CMD ["python", "-m", "uvicorn", "dashboard.backend.main:app", \
     "--host", "0.0.0.0", "--port", "8002", "--log-level", "info"]
