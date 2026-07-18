"""
Best-effort Decision Engine hand-off after an incident is created.

Why call Decision Engine from IM (not only Redis consumer)?
----------------------------------------------------------
The ticket id is known here. Decision Engine can attach incident_id for
RCA / remediation side effects without racing Redis dual-publish.

Failures never block ticket creation (same pattern as RCAClient).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from aiops_shared.models import AnomalyEvent, Incident

from app.config import settings

logger = logging.getLogger(__name__)


class DecisionClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        raw = base_url if base_url is not None else settings.decision_engine_url
        self.base_url = (raw or "").rstrip("/")
        self._http = httpx.Client(timeout=settings.decision_timeout_sec)
        self.pushed = 0
        self.last_error: Optional[str] = None

    def close(self) -> None:
        self._http.close()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url) and settings.enable_decision_engine

    def push(
        self,
        incident: Incident,
        anomaly: Optional[AnomalyEvent] = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "decision engine disabled"}

        ctx = dict(incident.context or {})
        if anomaly is not None:
            ctx = {**ctx, **(anomaly.context or {})}

        body = {
            "incident_id": incident.id,
            "anomaly_id": incident.source_anomaly_id or (anomaly.id if anomaly else None),
            "service_name": incident.service_name,
            "metric_name": incident.metric_name or "http_error_rate",
            "metric_value": float(incident.metric_value or 0.0),
            "anomaly_score": float(ctx.get("anomaly_score") or 0.0),
            "detection_method": str(ctx.get("detection_method") or ""),
            "explanation": str(
                ctx.get("explanation") or incident.description or ""
            )[:2000],
            "severity": incident.severity.value
            if hasattr(incident.severity, "value")
            else str(incident.severity),
            "confidence_score": float(ctx.get("confidence_score") or 50.0),
            "confidence_breakdown": dict(ctx.get("confidence_breakdown") or {}),
            "missing_context": list(ctx.get("missing_context") or []),
            "context_completeness": float(ctx.get("context_completeness") or 0.0),
            "signals": dict((ctx.get("signals") or {})),
            "primary_trace_id": ctx.get("primary_trace_id"),
            "features": dict(ctx.get("features") or {}),
            "skip_side_effects": False,
        }
        url = f"{self.base_url}/decide"
        try:
            resp = self._http.post(url, json=body)
            if resp.status_code >= 400:
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(
                    "decision push failed incident=%s status=%s",
                    incident.id,
                    resp.status_code,
                )
                return {"ok": False, "status_code": resp.status_code}
            self.pushed += 1
            self.last_error = None
            data = resp.json()
            action = ((data or {}).get("decision") or {}).get("action")
            logger.info(
                "decision push ok incident=%s action=%s",
                incident.id,
                action,
            )
            return {"ok": True, "response": data}
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("decision push error incident=%s: %s", incident.id, exc)
            return {"ok": False, "error": str(exc)}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url": self.base_url or None,
            "pushed": self.pushed,
            "last_error": self.last_error,
        }
