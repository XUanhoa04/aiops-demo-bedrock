"""
AIOps Feedback Collector — closes the AIOps loop.

On-call reviews incidents (anomaly / RCA / action thumbs + comment),
persists to SQLite, exports Prometheus quality metrics, and suggests
detector threshold tweaks when false-positive rate is high.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.metrics import refresh_gauges
from app.models import FeedbackCreate, FeedbackRecord, FeedbackStats, TuningSuggestion
from app.service import FeedbackService
from app.tuning import format_tuning_report

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

svc = FeedbackService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    refresh_gauges(svc.repo.compute_stats())
    logger.info(
        "feedback-collector ready db=%s streamlit=:%s",
        settings.feedback_db_path,
        settings.streamlit_port,
    )
    yield
    svc.close()


app = FastAPI(
    title="AIOps Feedback Collector",
    description=(
        "On-call review: thumbs for anomaly/RCA/action, comments, SQLite history, "
        "Prometheus quality metrics, threshold tuning hints."
    ),
    version="0.2.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    im_ok = svc.incidents.healthy()
    stats = svc.stats()
    return HealthResponse(
        status="ok" if im_ok else "degraded",
        service=settings.service_name,
        version="0.2.0",
        details={
            "incident_manager_ok": im_ok,
            "db_path": settings.feedback_db_path,
            "streamlit_port": settings.streamlit_port,
            "stats": stats.model_dump(),
        },
    )


@app.get("/ready")
def ready() -> dict[str, Any]:
    if not svc.incidents.healthy():
        raise HTTPException(status_code=503, detail="incident-manager unavailable")
    return {"ready": True}


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape — feedback_positive_rate, rca_accuracy_estimate, false_positive_count."""
    refresh_gauges(svc.repo.compute_stats())
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Feedback CRUD
# ---------------------------------------------------------------------------


@app.post("/feedback", response_model=FeedbackRecord, status_code=201)
def submit_feedback(body: FeedbackCreate) -> FeedbackRecord:
    """Submit on-call thumbs + comment for an incident."""
    if (
        body.anomaly_correct is None
        and body.rca_useful is None
        and body.action_effective is None
        and not (body.comment or "").strip()
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one thumb vote or a comment",
        )
    return svc.submit(body)


@app.get("/feedback", response_model=list[FeedbackRecord])
def list_feedback(
    incident_id: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> list[FeedbackRecord]:
    return svc.repo.list(incident_id=incident_id, limit=limit)


@app.get("/feedback/{feedback_id}", response_model=FeedbackRecord)
def get_feedback(feedback_id: str) -> FeedbackRecord:
    rec = svc.repo.get(feedback_id)
    if not rec:
        raise HTTPException(status_code=404, detail="feedback not found")
    return rec


@app.get("/stats", response_model=FeedbackStats)
def stats() -> FeedbackStats:
    return svc.stats()


@app.get("/tuning/suggestions", response_model=TuningSuggestion)
def tuning_suggestions() -> TuningSuggestion:
    """Heuristic threshold adjustments when FP rate is high."""
    return svc.tuning()


@app.get("/tuning/report", response_class=Response)
def tuning_report() -> Response:
    text = format_tuning_report(svc.tuning())
    return Response(content=text, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------------------
# Incidents proxy for Streamlit
# ---------------------------------------------------------------------------


@app.get("/incidents")
def list_incidents(limit: int = Query(default=25, ge=1, le=100)) -> list[dict[str, Any]]:
    try:
        incidents = svc.incidents.list_incidents(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # Attach latest feedback per incident
    out: list[dict[str, Any]] = []
    for inc in incidents:
        iid = inc.get("id") or ""
        fb = svc.repo.list(incident_id=iid, limit=3)
        out.append(
            {
                "incident": inc,
                "feedback": [f.model_dump(mode="json") for f in fb],
                "reviewed": len(fb) > 0,
            }
        )
    return out


@app.get("/incidents/{incident_id}")
def get_incident_bundle(incident_id: str) -> dict[str, Any]:
    try:
        inc = svc.incidents.get_incident(incident_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    fb = svc.repo.list(incident_id=incident_id, limit=20)
    return {
        "incident": inc,
        "feedback": [f.model_dump(mode="json") for f in fb],
        "reviewed": len(fb) > 0,
    }
