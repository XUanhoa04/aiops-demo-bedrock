# =============================================================================
# AIOps Anomaly Detector
#
# Production choices:
# - python:3.12-slim + non-root user
# - monorepo shared package copied in (no private PyPI)
# - ML deps (numpy/pandas/sklearn) installed from service requirements.txt
# - single-worker uvicorn is enough for control-plane poll + API
# =============================================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    # Sensible CPU defaults for sklearn inside a small container
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY shared/requirements-base.txt /app/shared/requirements-base.txt
COPY shared/aiops_shared /app/aiops_shared
COPY aiops-services/anomaly-detector/requirements.txt /app/requirements.txt
COPY aiops-services/anomaly-detector/app /app/app

RUN pip install --upgrade pip \
    && pip install -r /app/shared/requirements-base.txt \
    && pip install -r /app/requirements.txt

USER appuser

EXPOSE 8001

# /metrics + /health used by compose healthcheck / Prometheus scrape
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
