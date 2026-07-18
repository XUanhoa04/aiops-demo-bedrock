"""HTTP clients for backend AIOps services (compose DNS or localhost)."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx

IM = os.getenv("INCIDENT_MANAGER_URL", "http://aiops-incident-manager:8002").rstrip("/")
RCA = os.getenv("RCA_ENGINE_URL", "http://aiops-rca-engine:8003").rstrip("/")
REM = os.getenv("REMEDIATION_URL", "http://aiops-remediation:8004").rstrip("/")
FB = os.getenv("FEEDBACK_URL", "http://aiops-feedback-collector:8005").rstrip("/")
DET = os.getenv("ANOMALY_DETECTOR_URL", "http://aiops-anomaly-detector:8001").rstrip("/")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SEC", "20"))


def _req(method: str, url: str, **kwargs: Any) -> Any:
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.request(method, url, **kwargs)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} → {r.status_code}: {r.text[:500]}")
        if not r.content:
            return None
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            return r.json()
        return r.text


def health(base: str) -> dict[str, Any]:
    try:
        return _req("GET", f"{base}/health")
    except Exception as exc:
        return {"status": "down", "error": str(exc), "service": base}


def list_incidents(limit: int = 50, status: Optional[str] = None) -> list[dict]:
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    data = _req("GET", f"{IM}/incidents", params=params)
    return data if isinstance(data, list) else []


def get_incident(incident_id: str) -> dict:
    return _req("GET", f"{IM}/incidents/{incident_id}")


def observability_links(incident_id: str) -> dict:
    return _req("GET", f"{IM}/incidents/{incident_id}/observability-links")


def im_stats() -> dict:
    return _req("GET", f"{IM}/stats")


def run_rca(incident_id: str, force: bool = True) -> dict:
    return _req(
        "POST",
        f"{RCA}/analyze-incident/{incident_id}",
        params={"force": str(force).lower(), "persist": "true"},
    )


def propose_remediation(incident_id: str, actions: Optional[list[str]] = None) -> list:
    body = {"incident_id": incident_id, "actions": actions or []}
    data = _req("POST", f"{REM}/remediate/propose", json=body)
    return data if isinstance(data, list) else []


def list_actions(incident_id: str) -> list:
    data = _req("GET", f"{REM}/actions", params={"incident_id": incident_id, "limit": 30})
    return data if isinstance(data, list) else []


def submit_feedback(payload: dict) -> dict:
    return _req("POST", f"{FB}/feedback", json=payload)


def feedback_stats() -> dict:
    return _req("GET", f"{FB}/stats")


def feedback_list(incident_id: Optional[str] = None, limit: int = 20) -> list:
    params: dict[str, Any] = {"limit": limit}
    if incident_id:
        params["incident_id"] = incident_id
    data = _req("GET", f"{FB}/feedback", params=params)
    return data if isinstance(data, list) else []


def tuning_suggestions() -> dict:
    return _req("GET", f"{FB}/tuning/suggestions")


def detector_status() -> dict:
    try:
        return _req("GET", f"{DET}/status")
    except Exception:
        return health(DET)


def parse_notes(raw: Any) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        if str(raw).strip().startswith("{"):
            return json.loads(raw)
    except Exception:
        pass
    return {"raw": raw}


def explanation_of(inc: dict) -> str:
    ctx = inc.get("context") or {}
    return (
        ctx.get("explanation")
        or (ctx.get("explainability") or {}).get("summary")
        or (ctx.get("anomaly_details") or {}).get("message")
        or inc.get("description")
        or "—"
    )
