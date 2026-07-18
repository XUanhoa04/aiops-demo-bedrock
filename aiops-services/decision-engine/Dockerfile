# AIOps Decision Engine — confidence-gated routing (RCA / remediate / escalate)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY shared/requirements-base.txt /app/shared/requirements-base.txt
COPY shared/aiops_shared /app/aiops_shared
COPY aiops-services/decision-engine/requirements.txt /app/requirements.txt
COPY aiops-services/decision-engine/app /app/app

RUN pip install --upgrade pip \
    && pip install -r /app/shared/requirements-base.txt \
    && pip install -r /app/requirements.txt

USER appuser
EXPOSE 8006

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8006"]
