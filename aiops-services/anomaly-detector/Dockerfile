# Production choices:
# - slim Python base keeps image small for demos
# - copy shared package into image (monorepo pattern without private registry)
# - non-root user reduces blast radius if container is compromised
# - single worker uvicorn is fine for this control-plane service

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# System CA + curl for health/debug (optional curl not required for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY shared/requirements-base.txt /app/shared/requirements-base.txt
COPY shared/aiops_shared /app/aiops_shared
COPY aiops-services/anomaly-detector/requirements.txt /app/requirements.txt
COPY aiops-services/anomaly-detector/app /app/app

RUN pip install --upgrade pip \
    && pip install -r /app/shared/requirements-base.txt

USER appuser

EXPOSE 8001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
