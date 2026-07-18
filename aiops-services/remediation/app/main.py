"""
AIOps Remediation API — risk-gated actions with approval workflow.

Production notes
----------------
* Low-risk auto: reversible demo ops (chaos reset, log-only).
* High-risk: restart/scale require Approve & Execute (or force override).
* Action history is append-mostly SQLite for audit (who did what, when).
* Streamlit UI on :8501 talks to this API on :8004.
* When REMEDIATION_API_KEY is set, approve/execute/reject/FP require X-API-Key.
"""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

from app.config import settings
from app.models import (
    ActionRecord,
    ApproveRequest,
    ExecuteRequest,
    FalsePositiveRequest,
    IncidentBundle,
    ProposeRequest,
)
from app.service import RemediationService

setup_logging(settings.log_level)
logger = logging.getLogger(__name__)

svc = RemediationService()


def require_operator_auth(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Gate high-impact mutations when REMEDIATION_API_KEY is configured.

    Empty key = open localhost demo (documented on /health as auth_required=false).
    Uses secrets.compare_digest to avoid timing leaks on the demo token.
    """
    expected = (settings.remediation_api_key or "").strip()
    if not expected:
        return
    provided = (x_api_key or "").strip()
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid X-API-Key (set REMEDIATION_API_KEY)",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth_on = bool((settings.remediation_api_key or "").strip())
    logger.info(
        "remediation ready db=%s auto_low=%s simulate_only=%s auth_required=%s",
        settings.remediation_db_path,
        settings.auto_execute_low_risk,
        settings.simulate_only,
        auth_on,
    )
    if not auth_on:
        logger.warning(
            "REMEDIATION_API_KEY empty — approve/execute open on localhost "
            "(demo mode). Set a key for production-like safety."
        )
    yield
    svc.close()


app = FastAPI(
    title="AIOps Remediation",
    description=(
        "Propose remediation from RCA suggested_actions, gate high-risk "
        "behind approval, execute/simulate restart & scale, store audit history."
    ),
    version="0.3.0",
    lifespan=lifespan,
)
setup_otel(settings.service_name, app=app)

# Streamlit (browser) → API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    im_ok = svc.incidents.healthy()
    auth_on = bool((settings.remediation_api_key or "").strip())
    return HealthResponse(
        status="ok" if im_ok else "degraded",
        service=settings.service_name,
        version="0.3.0",
        details={
            "incident_manager_ok": im_ok,
            "db_path": settings.remediation_db_path,
            "auto_execute_low_risk": settings.auto_execute_low_risk,
            "simulate_only": settings.simulate_only,
            "actions_by_status": svc.repo.count_by_status(),
            "streamlit_port": settings.streamlit_port,
            "auth_required": auth_on,
        },
    )


@app.get("/ready")
def ready() -> dict[str, Any]:
    if not svc.incidents.healthy():
        raise HTTPException(status_code=503, detail="incident-manager unavailable")
    return {"ready": True}


# ---------------------------------------------------------------------------
# Incidents (proxy + RCA bundle for UI)
# ---------------------------------------------------------------------------


@app.get("/incidents", response_model=list[IncidentBundle])
def list_incident_bundles(
    limit: int = Query(default=20, ge=1, le=100),
) -> list[IncidentBundle]:
    try:
        return svc.list_bundles(limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/incidents/{incident_id}", response_model=IncidentBundle)
def get_incident_bundle(incident_id: str) -> IncidentBundle:
    try:
        return svc.get_bundle(incident_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Propose / approve / execute
# ---------------------------------------------------------------------------


@app.post("/remediate/propose", response_model=list[ActionRecord])
def propose(body: ProposeRequest) -> list[ActionRecord]:
    """
    Build action records from RCA suggested_actions (or explicit list).

    Low-risk may auto-execute when AUTO_EXECUTE_LOW_RISK=true.
    High-risk stay proposed until Approve.
    """
    try:
        return svc.propose_for_incident(
            body.incident_id,
            body.actions or None,
            auto_execute_low_risk=body.auto_execute_low_risk,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/actions", response_model=list[ActionRecord])
def list_actions(
    incident_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ActionRecord]:
    return svc.repo.list(incident_id=incident_id, status=status, limit=limit)


@app.get("/actions/{action_id}", response_model=ActionRecord)
def get_action(action_id: str) -> ActionRecord:
    rec = svc.repo.get(action_id)
    if not rec:
        raise HTTPException(status_code=404, detail="action not found")
    return rec


@app.post("/actions/{action_id}/approve", response_model=ActionRecord)
def approve_action(
    action_id: str,
    body: ApproveRequest,
    _: None = Depends(require_operator_auth),
) -> ActionRecord:
    """Approve high-risk action; optionally execute immediately (default)."""
    try:
        return svc.approve(
            action_id,
            executed_by=body.executed_by,
            execute_now=body.execute_now,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="action not found")


@app.post("/actions/{action_id}/execute", response_model=ActionRecord)
def execute_action(
    action_id: str,
    body: ExecuteRequest,
    _: None = Depends(require_operator_auth),
) -> ActionRecord:
    try:
        return svc.execute(
            action_id,
            executed_by=body.executed_by,
            force=body.force,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="action not found")


@app.post("/actions/{action_id}/reject", response_model=ActionRecord)
def reject_action(
    action_id: str,
    executed_by: str = "operator",
    reason: str = "rejected",
    _: None = Depends(require_operator_auth),
) -> ActionRecord:
    try:
        return svc.reject(action_id, executed_by=executed_by, reason=reason)
    except LookupError:
        raise HTTPException(status_code=404, detail="action not found")


@app.post("/incidents/{incident_id}/false-positive")
def false_positive(
    incident_id: str,
    body: FalsePositiveRequest,
    _: None = Depends(require_operator_auth),
) -> dict[str, Any]:
    """Mark incident as false_positive + audit row."""
    try:
        rec, incident = svc.mark_false_positive(
            incident_id,
            executed_by=body.executed_by,
            note=body.note,
        )
        return {"action": rec, "incident": incident}
    except LookupError:
        raise HTTPException(status_code=404, detail="incident not found")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Legacy stub path used by older demos
@app.post("/remediate", response_model=ActionRecord)
def remediate_legacy(
    incident_id: str,
    action_type: str = "reset_error_rate",
    target_service: str = "checkout-service",
    executed_by: str = "legacy-api",
    _: None = Depends(require_operator_auth),
) -> ActionRecord:
    """Backward-compatible one-shot remediate (low-risk chaos reset)."""
    texts = {
        "reset_error_rate": f"Reset error_rate chaos on {target_service}",
        "reset_latency": f"Reset latency chaos on {target_service}",
        "restart_service": f"Restart service {target_service}",
        "scale_deployment": f"Scale deployment {target_service} to 2",
    }
    text = texts.get(action_type, f"{action_type} on {target_service}")
    created = svc.propose_for_incident(
        incident_id,
        [text],
        auto_execute_low_risk=True,
    )
    if not created:
        raise HTTPException(status_code=400, detail="no action created")
    rec = created[0]
    if rec.risk_level.value == "high" and rec.status.value == "proposed":
        rec = svc.approve(rec.id, executed_by=executed_by, execute_now=True)
    return rec


@app.get("/stats")
def stats() -> dict[str, Any]:
    return {
        "actions_by_status": svc.repo.count_by_status(),
        "auto_execute_low_risk": settings.auto_execute_low_risk,
        "simulate_only": settings.simulate_only,
        "db_path": settings.remediation_db_path,
    }
