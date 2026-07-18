FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    STREAMLIT_PORT=8500

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY aiops-services/aiops-console/requirements.txt /app/requirements.txt
COPY aiops-services/aiops-console/app /app/app

RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt \
    && chown -R appuser:appuser /app

USER appuser
EXPOSE 8500

# Lightweight health for compose: Streamlit exposes HTTP on 8500
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=8 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8500/', timeout=3)"

CMD ["streamlit", "run", "app/streamlit_app.py", \
     "--server.address", "0.0.0.0", \
     "--server.port", "8500", \
     "--server.headless", "true", \
     "--browser.gatherUsageStats", "false"]
