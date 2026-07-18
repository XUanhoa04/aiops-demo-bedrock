FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORT=8005 \
    STREAMLIT_PORT=8502 \
    FEEDBACK_API_URL=http://127.0.0.1:8005

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data

COPY shared/requirements-base.txt /app/shared/requirements-base.txt
COPY shared/aiops_shared /app/aiops_shared
COPY aiops-services/feedback-collector/requirements.txt /app/requirements.txt
COPY aiops-services/feedback-collector/app /app/app
COPY aiops-services/feedback-collector/start.sh /app/start.sh

RUN pip install --upgrade pip \
    && pip install -r /app/shared/requirements-base.txt \
    && pip install -r /app/requirements.txt \
    && chmod +x /app/start.sh \
    && chown -R appuser:appuser /app

USER appuser
VOLUME ["/data"]
EXPOSE 8005 8502

CMD ["/app/start.sh"]
