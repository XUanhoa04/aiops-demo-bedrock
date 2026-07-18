"""
AIOps RCA Engine — grounded Bedrock root-cause analysis.

Production notes
----------------
* **Why ground?** LLMs are fluent liars under uncertainty. Feeding Prom/Loki/Tempo
  facts (and forbidding claims outside that set) turns RCA into *evidence-bound*
  synthesis instead of free-form storytelling that misleads on-call.
* **Why structured JSON?** Downstream automation (remediation, ticketing, UI)
  needs machine-readable fields. Free text forces another LLM step and is hard
  to validate. Schema validation rejects bad shapes before PATCH.
* **Why low temperature (0.1–0.3)?** Prefer stable, conservative wording over
  creative hypotheses when the cost of a wrong root cause is wasted engineer time.
* **Why rule fallback?** Bedrock outages / throttle / bad keys must not black-hole
  the pipeline; deterministic heuristics keep demos and degraded prod usable.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.consumer import IncidentConsumer
from app.engine import RCAEngine
from app.models import AnalyzeResponse, RCAResult

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

engine = RCAEngine()
consumer = IncidentConsumer(engine)


def _bg_analyze(incident_id: str, force: bool, persist: bool) -> None:
    try:
        resp = engine.analyze_incident(incident_id, persist=persist, force=force)
        logger.info(
            "background RCA done incident=%s status=%s mode=%s",
            incident_id,
            resp.status,
            resp.mode,
        )
    except Exception as exc:
        logger.exception("background RCA failed incident=%s: %s", incident_id, exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await consumer.start()
    logger.info(
        "rca-engine ready model=%s region=%s bedrock_configured=%s",
        settings.bedrock_model_id,
        settings.aws_default_region,
        engine.bedrock.configured,
    )
    yield
    await consumer.stop()
    engine.close()


app = FastAPI(
    title="AIOps RCA Engine",
    description=(
        "Grounded root-cause analysis: Prometheus + Loki + Tempo evidence pack → "
        "Amazon Bedrock Converse (JSON RCA) → persist on Incident Manager. "
        "Rule-based fallback when Bedrock is unavailable."
    ),
    version="0.2.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)


class RCARequest(BaseModel):
    """Webhook body from Incident Manager (`POST /rca/analyze`)."""

    incident_id: str = Field(..., examples=["3c5d0f7f-4787-4ebb-b300-57085094c251"])
    force: bool = Field(default=False, description="Re-run even if recent RCA exists")
    persist: bool = Field(default=True, description="PATCH result back to incident-manager")
    # IM webhook should not block on Bedrock latency — default async queue
    wait: bool = Field(
        default=False,
        description="If false (default), analyze in background and return immediately",
    )


class EvidencePreviewRequest(BaseModel):
    incident_id: str


# ---------------------------------------------------------------------------
# Health / ops
# ---------------------------------------------------------------------------


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    st = engine.status()
    obs = st.get("observability") or {}
    im_ok = bool(st.get("incident_manager_ok"))
    bedrock = st.get("bedrock") or {}
    degraded = not im_ok
    return HealthResponse(
        status="degraded" if degraded else "ok",
        service=settings.service_name,
        version="0.2.0",
        details={
            "mode": "bedrock" if bedrock.get("configured") else "rule_based",
            "bedrock": bedrock,
            "observability": obs,
            "incident_manager_ok": im_ok,
            "consumer": consumer.status(),
            "analyzed": st.get("analyzed"),
            "evidence_window_minutes": settings.evidence_window_minutes,
            "min_bedrock_confidence": settings.min_bedrock_confidence,
            "bedrock_temperature": settings.bedrock_temperature,
            "grafana_public_url": settings.grafana_public_url,
        },
    )


@app.get("/ready")
def ready() -> dict[str, Any]:
    if not engine.incidents.healthy():
        raise HTTPException(status_code=503, detail="incident-manager unavailable")
    return {"ready": True}


# ---------------------------------------------------------------------------
# RCA triggers
# ---------------------------------------------------------------------------


@app.post("/rca/analyze", response_model=AnalyzeResponse)
def rca_analyze(body: RCARequest, background_tasks: BackgroundTasks) -> AnalyzeResponse:
    """
    Primary webhook used by Incident Manager when a new ticket is created.

    Default wait=false → background task so IM is not blocked by Bedrock latency.
    Redis consumer also triggers the same engine path.
    """
    if not body.wait:
        background_tasks.add_task(_bg_analyze, body.incident_id, body.force, body.persist)
        return AnalyzeResponse(
            incident_id=body.incident_id,
            status="ok",
            mode="skipped",
            result=RCAResult(
                root_cause="RCA queued",
                confidence=0,
                affected_components=[],
                evidence=["async webhook accepted"],
                suggested_actions=[],
                runbook_suggestion="",
            ),
            persisted=False,
            message="RCA analysis queued (background)",
        )
    return engine.analyze_incident(
        body.incident_id,
        persist=body.persist,
        force=body.force,
    )


@app.post("/analyze-incident/{incident_id}", response_model=AnalyzeResponse)
@app.get("/analyze-incident/{incident_id}", response_model=AnalyzeResponse)
def analyze_incident(
    incident_id: str,
    force: bool = Query(default=True, description="Re-run even if RCA exists"),
    persist: bool = Query(default=True),
) -> AnalyzeResponse:
    """
    Test / ops endpoint: run full grounded RCA for an existing incident id.

    Example:
      curl -X POST http://localhost:8003/analyze-incident/<id>?force=true
    """
    return engine.analyze_incident(incident_id, persist=persist, force=force)


@app.post("/rca/preview-evidence")
def preview_evidence(body: EvidencePreviewRequest) -> dict[str, Any]:
    """Return the evidence pack without calling Bedrock (debug grounding)."""
    try:
        incident = engine.incidents.get_incident(body.incident_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    pack = engine.gatherer.gather(incident)
    return {
        "incident_id": pack.incident_id,
        "service_name": pack.service_name,
        "window": {
            "minutes": pack.window_minutes,
            "start": pack.window_start_iso,
            "end": pack.window_end_iso,
        },
        "sources_ok": pack.sources_ok,
        "gather_errors": pack.gather_errors,
        "metrics_summary": pack.metrics_summary,
        "error_logs": pack.error_logs,
        "traces": pack.traces,
        "prompt_preview_chars": len(pack.to_prompt_block()),
    }


@app.get("/stats")
def stats() -> dict[str, Any]:
    return {
        "engine": engine.status(),
        "consumer": consumer.status(),
        "settings": {
            "bedrock_model_id": settings.bedrock_model_id,
            "aws_region": settings.aws_default_region,
            "temperature": settings.bedrock_temperature,
            "max_tokens": settings.bedrock_max_tokens,
            "evidence_window_minutes": settings.evidence_window_minutes,
            "force_rule_based": settings.force_rule_based,
        },
    }
