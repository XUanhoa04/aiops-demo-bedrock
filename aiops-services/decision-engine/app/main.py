"""
AIOps Decision Engine — FastAPI entrypoint.

Routes anomaly confidence into:
  * AUTO_REMEDIATE_GATED  (conf ≥ 85 + known pattern)
  * RCA_SUGGEST           (60–85 → Bedrock via RCA, limited)
  * ESCALATE_ONCALL       (< 60 or missing critical context)

See app/decision_table.py and README.md for the full decision table.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.consumer import DecisionConsumer
from app.decision_table import table_as_markdown
from app.engine import DecisionEngine
from app.models import (
    AnomalyEventIn,
    DecideRequest,
    EngineDecision,
    anomaly_event_to_request,
)

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

engine = DecisionEngine()
consumer = DecisionConsumer(engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await consumer.start()
    logger.info(
        "decision-engine ready port=%s high=%.0f medium=%.0f max_iter=%s",
        settings.port,
        settings.confidence_high,
        settings.confidence_medium,
        settings.max_iterations,
    )
    yield
    await consumer.stop()
    engine.close()


app = FastAPI(
    title="AIOps Decision Engine",
    description=(
        "Confidence-gated routing: auto-remediate (gated) / Bedrock RCA / "
        "escalate on-call. Limited iteration loop (max 2–3). "
        "See GET /decision-table."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)


class FromAnomalyBody(BaseModel):
    event: AnomalyEventIn
    incident_id: Optional[str] = None
    skip_side_effects: bool = False


class DecideResponse(BaseModel):
    decision: EngineDecision
    message: str = "ok"


# ---------------------------------------------------------------------------
# Health / metrics / table
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    st = engine.status()
    cst = consumer.status()
    redis_ok = cst.get("redis_ok", True)
    status = "ok" if redis_ok or not settings.enable_redis_consumer else "degraded"
    return HealthResponse(
        status=status,
        service=settings.service_name,
        version="0.1.0",
        details={"engine": st, "consumer": cst},
    )


@app.get("/ready")
def ready() -> dict[str, Any]:
    return {"ready": True, "decided": engine.decided}


@app.get("/metrics")
def metrics() -> Response:
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/decision-table", response_class=PlainTextResponse)
def decision_table() -> str:
    """Human-readable policy matrix (also in README)."""
    return table_as_markdown()


@app.get("/config")
def get_config() -> dict[str, Any]:
    return {
        "confidence_high": settings.confidence_high,
        "confidence_medium": settings.confidence_medium,
        "max_iterations": settings.max_iterations,
        "critical_missing_context": sorted(settings.critical_missing_set),
        "enable_llm": settings.enable_llm,
        "enable_auto_remediation": settings.enable_auto_remediation,
        "auto_execute_gated_low_risk": settings.auto_execute_gated_low_risk,
        "min_llm_confidence": settings.min_llm_confidence,
        "anomaly_detector_url": settings.anomaly_detector_url,
        "incident_manager_url": settings.incident_manager_url,
        "rca_engine_url": settings.rca_engine_url,
        "remediation_url": settings.remediation_url,
        "redis_queue_decisions": settings.redis_queue_decisions,
    }


# ---------------------------------------------------------------------------
# Decide APIs
# ---------------------------------------------------------------------------


@app.post("/decide", response_model=DecideResponse)
def decide(body: DecideRequest) -> DecideResponse:
    """
    Primary API: accept Confidence Scorer output (+ optional signals) and route.
    """
    try:
        decision = engine.decide(body)
    except Exception as exc:
        logger.exception("decide failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return DecideResponse(
        decision=decision,
        message=f"action={decision.action.value} band={decision.band.value}",
    )


@app.post("/decide/from-anomaly", response_model=DecideResponse)
def decide_from_anomaly(body: FromAnomalyBody) -> DecideResponse:
    """Map AnomalyEvent (detector Redis payload) → DecideRequest → decide."""
    req = anomaly_event_to_request(body.event, incident_id=body.incident_id)
    req.skip_side_effects = body.skip_side_effects
    try:
        decision = engine.decide(req)
    except Exception as exc:
        logger.exception("decide/from-anomaly failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return DecideResponse(decision=decision, message=decision.action.value)


@app.get("/decisions", response_model=list[EngineDecision])
def list_decisions(limit: int = Query(default=20, ge=1, le=100)) -> list[EngineDecision]:
    return engine.recent[:limit]


@app.get("/decisions/{decision_id}", response_model=EngineDecision)
def get_decision(decision_id: str) -> EngineDecision:
    for d in engine.recent:
        if d.id == decision_id:
            return d
    raise HTTPException(status_code=404, detail="decision not found")


@app.get("/status")
def status() -> dict[str, Any]:
    return {"engine": engine.status(), "consumer": consumer.status()}
