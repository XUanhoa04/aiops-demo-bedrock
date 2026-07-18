"""
AIOps Anomaly Detector — FastAPI entrypoint.

Architecture
------------
  Prometheus (LGTM :9090)
        │  pull every DETECTION_INTERVAL (default 30s)
        ▼
  Feature extract (request rate, error rate, latency p95/p99)
        ▼
  Hybrid engine
      ├─ EWMA residual z-score
      ├─ Rolling mean/std z-score
      ├─ STL residual z-score (if seasonality)
      ├─ IsolationForest (multivariate)
      └─ Absolute thresholds (cold-start safety)
        ▼
  Multi-signal context (parallel)
      ├─ Metrics (Prom)
      ├─ Logs (Loki)
      ├─ Traces (Tempo)
      └─ Events (derived / change markers)
        ▼
  Confidence Scoring Engine (0–100)
        ▼
  DetectionDecision → Decision Engine / Redis / webhook
  Expose: anomaly_score, anomaly_confidence_score, context_completeness

Production notes
----------------
* **Why hybrid?** Explainable stats for on-call + ML for joint outliers.
* **Why confidence?** Gate auto-ticket / auto-remediate on multi-signal trust.
* **/metrics** is scrape-friendly for LGTM/Prometheus (prometheus_client).
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import AnomalyEvent, HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.models import DetectionDecision
from app.worker import DetectorWorker

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

worker = DetectorWorker()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await worker.start()
    logger.info(
        "anomaly-detector ready port=%s prometheus=%s loki=%s tempo=%s interval=%ss",
        settings.port,
        settings.prometheus_url,
        settings.loki_url,
        settings.tempo_url,
        settings.poll_interval_sec,
    )
    yield
    await worker.stop()


app = FastAPI(
    title="AIOps Anomaly Detector",
    description=(
        "Hybrid anomaly detection (EWMA + Z-score + STL + IsolationForest) "
        "with multi-signal context gathering and confidence scoring (0–100). "
        "Exposes anomaly_confidence_score + context_completeness for the Decision Engine."
    ),
    version="0.3.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DetectRequest(BaseModel):
    """Manual inject for demos, chaos validation, and unit-style checks."""

    service_name: str = Field(..., examples=["checkout-service"])
    metric_name: str = Field(default="http_error_rate", examples=["http_error_rate"])
    metric_value: float = Field(..., examples=[0.45], description="Observed value")
    threshold: float = Field(
        default=0.15,
        examples=[0.15],
        description="Absolute threshold for the manual detector path",
    )
    gather_context: bool = Field(
        default=True,
        description="Pull Loki/Tempo/Prom context for confidence scoring",
    )


class DetectResponse(BaseModel):
    event: AnomalyEvent
    decision: DetectionDecision
    message: str = "anomaly published"


class ScoreRequest(BaseModel):
    """
    Score an already-known anomaly with optional pre-fetched signals
    (useful for Decision Engine unit tests without live Loki/Tempo).
    """

    service_name: str
    metric_name: str = "http_error_rate"
    metric_value: float
    anomaly_score: float = 3.0
    is_anomaly: bool = True
    winning_methods: list[str] = Field(default_factory=lambda: ["ewma_zscore"])
    features: dict[str, float] = Field(default_factory=dict)
    gather_context: bool = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness — must stay cheap (compose/k8s probes)."""
    st = worker.status(deep=False)
    status = "ok" if st["redis_ok"] else "degraded"
    return HealthResponse(status=status, service=settings.service_name, details=st)


@app.get("/ready")
def ready() -> dict[str, Any]:
    st = worker.status(deep=False)
    if not st["redis_ok"]:
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"ready": True, "poll_count": st["poll_count"]}


@app.get("/metrics")
def metrics() -> Response:
    """
    Prometheus scrape endpoint.

    Series of interest for Grafana/alerts:
      anomaly_score{service,metric,method}
      is_anomaly{service,metric,method}
      detection_method{service,metric,method}
      anomaly_confidence_score{service,metric}
      context_completeness{service}
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/anomalies", response_model=list[AnomalyEvent])
def list_anomalies(limit: int = Query(default=20, ge=1, le=50)) -> list[AnomalyEvent]:
    return worker.recent_anomalies[:limit]


@app.get("/results")
def list_results(limit: int = Query(default=20, ge=1, le=100)) -> list[dict]:
    """Latest hybrid evaluation results (including non-anomalous)."""
    return worker.recent_results[:limit]


@app.get("/decisions", response_model=list[DetectionDecision])
def list_decisions(
    limit: int = Query(default=20, ge=1, le=50),
    anomalies_only: bool = Query(default=False),
) -> list[DetectionDecision]:
    """
    Recent DetectionDecision objects for the Decision Engine.

    Each item includes anomaly_score, detection_method, explanation,
    confidence_score, confidence_breakdown, missing_context, context_completeness.
    """
    items = worker.recent_decisions
    if anomalies_only:
        items = [d for d in items if d.is_anomaly]
    return items[:limit]


@app.get("/decisions/{decision_id}", response_model=DetectionDecision)
def get_decision(decision_id: str) -> DetectionDecision:
    for d in worker.recent_decisions:
        if d.id == decision_id:
            return d
    raise HTTPException(status_code=404, detail="decision not found")


@app.post("/detect", response_model=DetectResponse)
def manual_detect(body: DetectRequest) -> DetectResponse:
    """
    Force-run detection for a single observation and publish if anomalous.

    Used by `scripts/demo_flow.py` and live talks so you do not wait for
    PromQL series to populate. Returns full DetectionDecision for demos.
    """
    try:
        event, decision = worker.force_detect(
            service_name=body.service_name,
            metric_name=body.metric_name,
            metric_value=body.metric_value,
            threshold=body.threshold,
            gather_context=body.gather_context,
        )
    except Exception as exc:
        logger.exception("manual detect failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.warning(
        "manual detect service=%s metric=%s value=%s event_id=%s "
        "confidence=%.1f completeness=%.2f missing=%s",
        body.service_name,
        body.metric_name,
        body.metric_value,
        event.id,
        decision.confidence_score,
        decision.context_completeness,
        decision.missing_context,
    )
    return DetectResponse(event=event, decision=decision)


@app.post("/score", response_model=DetectionDecision)
def score_only(body: ScoreRequest) -> DetectionDecision:
    """
    Build a DetectionDecision without publishing (Decision Engine dry-run).

    Useful to inspect confidence_breakdown / missing_context.
    """
    from app.detector import HybridResult, MethodResult

    features = dict(body.features) or {body.metric_name: body.metric_value}
    methods = [
        MethodResult(
            method=m,
            score=body.anomaly_score,
            is_anomaly=body.is_anomaly,
            detail={
                "explanation": (
                    f"{body.metric_name}={body.metric_value:.4g} "
                    f"score={body.anomaly_score:.2f} via {m}"
                )
            },
        )
        for m in (body.winning_methods or ["ewma_zscore"])
    ]
    result = HybridResult(
        service=body.service_name,
        metric=body.metric_name,
        value=body.metric_value,
        is_anomaly=body.is_anomaly,
        anomaly_score=body.anomaly_score,
        methods=methods,
        features=features,
        winning_methods=list(body.winning_methods),
    )
    try:
        return worker.decisions.build(result, gather_context=body.gather_context)
    except Exception as exc:
        logger.exception("score failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/status")
def status() -> dict[str, Any]:
    return worker.status(deep=True)


@app.get("/config")
def get_config() -> dict[str, Any]:
    """Non-secret runtime config (for debugging demos)."""
    return {
        "prometheus_url": settings.prometheus_url,
        "loki_url": settings.loki_url,
        "tempo_url": settings.tempo_url,
        "detection_interval": settings.poll_interval_sec,
        "zscore_threshold": settings.zscore_threshold,
        "ewma_alpha": settings.ewma_alpha,
        "window_size": settings.window_size,
        "min_samples": settings.min_samples,
        "enable_stl": settings.enable_stl,
        "stl_period": settings.stl_period,
        "stl_min_seasonal_strength": settings.stl_min_seasonal_strength,
        "iforest_contamination": settings.iforest_contamination,
        "hybrid_vote": settings.hybrid_vote,
        "error_rate_threshold": settings.error_rate_threshold,
        "latency_p95_seconds_threshold": settings.latency_p95_seconds_threshold,
        "watched_services": settings.watched_service_list(),
        "enable_context_gather": settings.enable_context_gather,
        "context_window_minutes": settings.context_window_minutes,
        "confidence_weights": {
            "metrics": settings.confidence_weight_metrics,
            "traces": settings.confidence_weight_traces,
            "logs": settings.confidence_weight_logs,
            "events": settings.confidence_weight_events,
        },
        "penalties": {
            "missing_metrics": settings.penalty_missing_metrics,
            "missing_traces": settings.penalty_missing_traces,
            "missing_logs": settings.penalty_missing_logs,
            "missing_events": settings.penalty_missing_events,
            "source_down": settings.penalty_source_down,
        },
        "min_confidence_to_notify": settings.min_confidence_to_notify,
        "enable_redis_notify": settings.enable_redis_notify,
        "enable_webhook_notify": settings.enable_webhook_notify,
        "incident_webhook_url": settings.incident_webhook_url or None,
        "alert_cooldown_sec": settings.alert_cooldown_sec,
    }
