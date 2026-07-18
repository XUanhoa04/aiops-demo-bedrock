"""
AIOps Incident Manager — FastAPI + SQLite.

Consumes anomaly events from Redis, correlates, persists tickets, exposes REST API.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import (
    AnomalyEvent,
    AnomalySeverity,
    HealthResponse,
    Incident,
    IncidentStatus,
    utc_now,
)
from aiops_shared.otel import setup_otel

from app.config import settings
from app.consumer import AnomalyConsumer
from app.db import IncidentRepository

setup_logging()
logger = logging.getLogger(__name__)

repo = IncidentRepository()
consumer = AnomalyConsumer(repo)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await consumer.start()
    logger.info("incident-manager ready db=%s", settings.incident_db_path)
    yield
    await consumer.stop()


app = FastAPI(
    title="AIOps Incident Manager",
    description="Ticket store + anomaly consumer for the AIOps demo pipeline.",
    version="0.1.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)


class IncidentCreate(BaseModel):
    title: str
    description: str = ""
    service_name: str
    severity: AnomalySeverity = AnomalySeverity.MEDIUM
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None


class IncidentUpdate(BaseModel):
    status: Optional[IncidentStatus] = None
    description: Optional[str] = None
    root_cause: Optional[str] = None
    rca_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    remediation_notes: Optional[str] = None
    human_feedback: Optional[str] = None
    severity: Optional[AnomalySeverity] = None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    st = consumer.status()
    counts = repo.count_by_status()
    status = "ok" if st["redis_ok"] else "degraded"
    return HealthResponse(
        status=status,
        service=settings.service_name,
        details={**st, "incidents_by_status": counts, "db_path": settings.incident_db_path},
    )


@app.get("/ready")
def ready() -> dict:
    if not consumer.status()["redis_ok"]:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"ready": True}


@app.get("/incidents", response_model=list[Incident])
def list_incidents(
    status: Optional[IncidentStatus] = None,
    service_name: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[Incident]:
    return repo.list(
        status=status.value if status else None,
        service_name=service_name,
        limit=limit,
    )


@app.get("/incidents/{incident_id}", response_model=Incident)
def get_incident(incident_id: str) -> Incident:
    inc = repo.get(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    return inc


@app.post("/incidents", response_model=Incident, status_code=201)
def create_incident(body: IncidentCreate) -> Incident:
    inc = Incident(
        title=body.title,
        description=body.description,
        service_name=body.service_name,
        severity=body.severity,
        metric_name=body.metric_name,
        metric_value=body.metric_value,
    )
    return repo.insert(inc)


@app.patch("/incidents/{incident_id}", response_model=Incident)
def update_incident(incident_id: str, body: IncidentUpdate) -> Incident:
    inc = repo.get(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(inc, key, value)
    if body.status in (IncidentStatus.RESOLVED, IncidentStatus.CLOSED) and not inc.resolved_at:
        inc.resolved_at = utc_now()
    return repo.update(inc)


@app.post("/incidents/from-anomaly", response_model=Incident, status_code=201)
def create_from_anomaly(anomaly: AnomalyEvent) -> Incident:
    """Synchronous path used by scripts / tests without going through Redis."""
    return consumer.handle_anomaly(anomaly)


@app.get("/stats")
def stats() -> dict:
    return {
        "by_status": repo.count_by_status(),
        "consumer": consumer.status(),
        "bedrock_model_id": settings.bedrock_model_id,
        "aws_region": settings.aws_default_region,
        "bedrock_configured": bool(settings.aws_access_key_id),
    }
