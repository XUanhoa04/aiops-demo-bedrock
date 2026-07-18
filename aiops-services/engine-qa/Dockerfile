# AIOps Engine QA — API (:8007) + Streamlit meta-review UI (:8503)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORT=8007 \
    STREAMLIT_PORT=8503 \
    ENGINE_QA_API_URL=http://127.0.0.1:8007

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

COPY shared/requirements-base.txt /app/shared/requirements-base.txt
COPY shared/aiops_shared /app/aiops_shared
COPY aiops-services/engine-qa/requirements.txt /app/requirements.txt
COPY aiops-services/engine-qa/app /app/app
COPY aiops-services/engine-qa/start.sh /app/start.sh

RUN pip install --upgrade pip \
    && pip install -r /app/shared/requirements-base.txt \
    && pip install -r /app/requirements.txt \
    && chmod +x /app/start.sh \
    && chown -R appuser:appuser /app

USER appuser
VOLUME ["/data"]
EXPOSE 8007 8503

CMD ["/app/start.sh"]
