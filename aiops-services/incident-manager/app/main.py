"""
AIOps Incident Manager — FastAPI + SQLite.

Receives anomalies from:
  1. Redis queue (async consumer) — primary path from Anomaly Detector
  2. HTTP webhook POST /incidents/from-anomaly — sync path

Persists tickets, exposes REST + simple UI, Prometheus metrics, and
RCA / Decision Engine hand-off hooks.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
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

from aiops_shared.grafana_links import build_observability_links

from app.config import settings
from app.consumer import AnomalyConsumer
from app.db import IncidentRepository
from app.prom_metrics import record_created, set_open_incidents
from app.decision_client import DecisionClient
from app.rca_client import RCAClient
from app.ui import UI_HTML

setup_logging()
logger = logging.getLogger(__name__)

repo = IncidentRepository()
rca = RCAClient()
decision = DecisionClient()
consumer = AnomalyConsumer(repo, rca=rca, decision=decision)


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_open_incidents(repo.count_open())
    await consumer.start()
    logger.info(
        "incident-manager ready db=%s rca=%s decision=%s",
        settings.incident_db_path,
        settings.rca_engine_url or "(disabled)",
        settings.decision_engine_url or "(disabled)",
    )
    yield
    await consumer.stop()
    rca.close()
    decision.close()


app = FastAPI(
    title="AIOps Incident Manager",
    description=(
        "Ticket store + anomaly consumer for the AIOps demo pipeline. "
        "Ingest via Redis or webhook; SQLite persistence; Prometheus metrics; "
        "UI stub at GET /."
    ),
    version="0.2.0",
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


# ---------------------------------------------------------------------------
# Ops / health / metrics / UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def ui_home() -> HTMLResponse:
    """Simple ops UI stub (list / filter / create). Streamlit can replace later."""
    return HTMLResponse(content=UI_HTML)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    st = consumer.status()
    counts = repo.count_by_status()
    status = "ok" if st["redis_ok"] else "degraded"
    return HealthResponse(
        status=status,
        service=settings.service_name,
        details={
            **st,
            "incidents_by_status": counts,
            "open_incidents": repo.count_open(),
            "db_path": settings.incident_db_path,
        },
    )


@app.get("/ready")
def ready() -> dict:
    if not consumer.status()["redis_ok"]:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"ready": True}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint (#incidents_created, #open_incidents, …)."""
    # Keep gauge accurate even if no recent writes
    try:
        set_open_incidents(repo.count_open())
    except Exception:
        pass
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/stats")
def stats() -> dict:
    return {
        "by_status": repo.count_by_status(),
        "open_incidents": repo.count_open(),
        "consumer": consumer.status(),
        "rca": rca.status(),
        "bedrock_model_id": settings.bedrock_model_id,
        "aws_region": settings.aws_default_region,
        "bedrock_configured": bool(settings.aws_access_key_id),
    }


# ---------------------------------------------------------------------------
# Incident REST API
# ---------------------------------------------------------------------------


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


@app.get("/incidents/{incident_id}/observability-links")
def incident_observability_links(incident_id: str) -> dict:
    """
    One-click Grafana / Tempo / Loki deep-links for the Incident Console.

    Prefer primary_trace_id from RCA remediation_notes when present; otherwise
    fall back to service-scoped TraceQL Explore (still useful pre-RCA).
    """
    import json

    inc = repo.get(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="incident not found")

    primary_trace_id = None
    notes_raw = inc.remediation_notes or ""
    if isinstance(notes_raw, str) and notes_raw.strip().startswith("{"):
        try:
            notes = json.loads(notes_raw)
            primary_trace_id = notes.get("primary_trace_id")
        except Exception:
            pass

    links = build_observability_links(
        grafana_base=settings.grafana_public_url,
        service_name=inc.service_name,
        primary_trace_id=primary_trace_id,
        tempo_uid=settings.tempo_datasource_uid,
        loki_uid=settings.loki_datasource_uid,
    )
    # Also surface detector explainability for API consumers / Slack bots.
    links["explanation"] = (inc.context or {}).get("explanation") or inc.description
    links["root_cause"] = inc.root_cause
    links["service_name"] = inc.service_name
    links["incident_id"] = inc.id
    return links


@app.post("/incidents", response_model=Incident, status_code=201)
def create_incident(body: IncidentCreate) -> Incident:
    """Manual ticket creation (UI / ops / tests)."""
    inc = Incident(
        title=body.title,
        description=body.description,
        service_name=body.service_name,
        severity=body.severity,
        metric_name=body.metric_name,
        metric_value=body.metric_value,
        context={"source": "manual"},
    )
    repo.insert(inc)
    record_created(
        source="manual",
        severity=inc.severity.value,
        service=inc.service_name,
    )
    set_open_incidents(repo.count_open())
    # Manual tickets also fan-out to Decision Engine / RCA
    consumer.fanout_new_incident(inc)
    return inc


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
    updated = repo.update(inc)
    set_open_incidents(repo.count_open())
    return updated


@app.post("/incidents/from-anomaly", response_model=Incident, status_code=201)
def create_from_anomaly(anomaly: AnomalyEvent) -> Incident:
    """
    Webhook path used by Anomaly Detector (`INCIDENT_WEBHOOK_URL`).

    Also useful for scripts/tests without going through Redis.
    Correlation still applies (may return an existing ticket).
    """
    return consumer.handle_anomaly(anomaly, source="webhook")
