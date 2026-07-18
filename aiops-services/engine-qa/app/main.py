"""
AIOps Engine QA — FastAPI entrypoint.

"Supervise the supervisors": quantify detector / confidence / RCA / decision
quality from on-call labels, export Prometheus meta-SLOs, and suggest knob
changes (weights & thresholds) without auto-applying them.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.models import (
    EngineQualityMetrics,
    QADashboard,
    QAReview,
    QAReviewCreate,
    TuningAdvice,
)
from app.service import EngineQAService

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

svc = EngineQAService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "engine-qa ready port=%s streamlit=:%s db=%s",
        settings.port,
        settings.streamlit_port,
        settings.qa_db_path,
    )
    yield
    svc.close()


app = FastAPI(
    title="AIOps Engine QA",
    description=(
        "Meta-evaluation of AIOps Engine + LLM: on-call reviews for anomaly, "
        "confidence, RCA, decision; precision/FP/hallucination/iterations; "
        "tuning suggestions for weights & thresholds."
    ),
    version="0.1.0",
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
    probe = svc.clients.probe()
    q = svc.quality()
    return HealthResponse(
        status="ok",
        service=settings.service_name,
        version="0.1.0",
        details={
            "pipeline": probe,
            "reviews": q.total_reviews,
            "overall_health": q.overall_engine_health,
            "streamlit_port": settings.streamlit_port,
            "db_path": settings.qa_db_path,
        },
    )


@app.get("/ready")
def ready() -> dict[str, Any]:
    return {"ready": True, "reviews": svc.repo.count()}


@app.get("/metrics")
def metrics() -> Response:
    svc.quality()  # refresh gauges
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Reviews
# ---------------------------------------------------------------------------


@app.post("/qa/reviews", response_model=QAReview, status_code=201)
def submit_review(body: QAReviewCreate) -> QAReview:
    try:
        return svc.submit(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("submit review failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/qa/reviews", response_model=list[QAReview])
def list_reviews(limit: int = Query(default=50, ge=1, le=200)) -> list[QAReview]:
    return svc.list_reviews(limit=limit)


@app.get("/qa/reviews/{review_id}", response_model=QAReview)
def get_review(review_id: str) -> QAReview:
    rec = svc.repo.get(review_id)
    if not rec:
        raise HTTPException(status_code=404, detail="review not found")
    return rec


# ---------------------------------------------------------------------------
# Quality + tuning
# ---------------------------------------------------------------------------


@app.get("/qa/metrics", response_model=EngineQualityMetrics)
def qa_metrics() -> EngineQualityMetrics:
    """JSON quality aggregates (same numbers as Prometheus gauges)."""
    return svc.quality()


@app.get("/qa/tuning", response_model=TuningAdvice)
def qa_tuning() -> TuningAdvice:
    return svc.tuning()


@app.get("/qa/tuning/report", response_class=PlainTextResponse)
def qa_tuning_report() -> str:
    return svc.tuning_report()


@app.get("/qa/dashboard", response_model=QADashboard)
def qa_dashboard() -> QADashboard:
    return svc.dashboard()


@app.get("/qa/review-queue")
def review_queue(limit: int = Query(default=20, ge=1, le=50)) -> list[dict]:
    """Incidents + decision snapshots for Streamlit review UI."""
    return svc.review_bundle(limit=limit)


@app.get("/config")
def get_config() -> dict[str, Any]:
    return {
        "min_samples_for_tuning": settings.min_samples_for_tuning,
        "fp_rate_warn": settings.fp_rate_warn,
        "hallucination_rate_warn": settings.hallucination_rate_warn,
        "current_zscore_threshold": settings.current_zscore_threshold,
        "current_confidence_weights": {
            "metrics": settings.current_confidence_weight_metrics,
            "traces": settings.current_confidence_weight_traces,
            "logs": settings.current_confidence_weight_logs,
            "events": settings.current_confidence_weight_events,
        },
        "current_confidence_high": settings.current_confidence_high,
        "current_confidence_medium": settings.current_confidence_medium,
        "sync_feedback_collector": settings.sync_feedback_collector,
    }
