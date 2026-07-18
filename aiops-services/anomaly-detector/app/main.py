"""
AIOps Anomaly Detector — FastAPI entrypoint.

Pipeline: Prometheus metrics → rule/z-score engine → Redis anomaly queue.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import AnomalyEvent, HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.worker import DetectorWorker

setup_logging()
logger = logging.getLogger(__name__)

worker = DetectorWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await worker.start()
    logger.info("anomaly-detector ready on :%s", settings.port)
    yield
    await worker.stop()


app = FastAPI(
    title="AIOps Anomaly Detector",
    description="Polls Prometheus (LGTM), detects anomalies, enqueues events to Redis.",
    version="0.1.0",
    lifespan=lifespan,
)
# Instrument before startup so middleware can be registered cleanly.
setup_otel(settings.service_name, app=app)


class DetectRequest(BaseModel):
    service_name: str = Field(..., examples=["checkout-service"])
    metric_name: str = Field(default="http_error_rate")
    metric_value: float = Field(..., examples=[0.45])
    threshold: float = Field(default=0.15)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness: process up + Redis. Prometheus may lag during LGTM boot."""
    st = worker.status(deep=False)
    status = "ok" if st["redis_ok"] else "degraded"
    return HealthResponse(
        status=status,
        service=settings.service_name,
        details=st,
    )


@app.get("/ready")
def ready() -> dict:
    st = worker.status(deep=False)
    if not st["redis_ok"]:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"ready": True}


@app.get("/anomalies", response_model=list[AnomalyEvent])
def list_anomalies(limit: int = 20) -> list[AnomalyEvent]:
    return worker.recent_anomalies[: max(1, min(limit, 50))]


@app.post("/detect", response_model=AnomalyEvent)
def manual_detect(body: DetectRequest) -> AnomalyEvent:
    """Inject an anomaly for live demos without waiting for PromQL."""
    return worker.force_detect(
        service_name=body.service_name,
        metric_name=body.metric_name,
        metric_value=body.metric_value,
        threshold=body.threshold,
    )


@app.get("/status")
def status() -> dict:
    return worker.status(deep=True)
