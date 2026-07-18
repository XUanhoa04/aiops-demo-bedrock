"""HTTP client for Incident Manager (fetch ticket + persist RCA fields)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from aiops_shared.grafana_links import build_observability_links

from app.config import settings
from app.models import RCAResult

logger = logging.getLogger(__name__)


class IncidentClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.incident_manager_url).rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))

    def close(self) -> None:
        self._http.close()

    def healthy(self) -> bool:
        try:
            r = self._http.get(f"{self.base_url}/health")
            return r.status_code == 200
        except Exception:
            return False

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        r = self._http.get(f"{self.base_url}/incidents/{incident_id}")
        if r.status_code == 404:
            raise LookupError(f"incident not found: {incident_id}")
        r.raise_for_status()
        return r.json()

    def list_open(self, limit: int = 20) -> list[dict[str, Any]]:
        r = self._http.get(
            f"{self.base_url}/incidents",
            params={"status": "open", "limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def persist_rca(
        self,
        incident_id: str,
        result: RCAResult,
        *,
        mode: str,
        extra_notes: Optional[dict[str, Any]] = None,
        primary_trace_id: Optional[str] = None,
        service_name: Optional[str] = None,
        related_traces: Optional[list[dict[str, Any]]] = None,
        grafana_trace_url: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        PATCH incident-manager with root_cause + rca_confidence + observability links.

        Trace deep-links live in remediation_notes JSON so the Incident UI can
        render a one-click "🔍 Xem Trace" button without schema migrations.

        Grafana URL format (spec):
          /explore?orgId=1&left={"datasource":"Tempo","queries":[{"refId":"A",
            "queryType":"traceql","query":"<trace_id>"}]}
        """
        obs_links = build_observability_links(
            grafana_base=settings.grafana_public_url,
            service_name=service_name or "unknown",
            primary_trace_id=primary_trace_id,
            tempo_uid=settings.tempo_datasource_uid,
            loki_uid=settings.loki_datasource_uid,
            tempo_name=settings.tempo_datasource_name,
            loki_name=settings.loki_datasource_name,
        )
        trace_url = grafana_trace_url or obs_links.get("primary_trace_url")
        # Prefer root_cause that includes explainability for list views
        root = result.root_cause
        if result.why_root_cause and "why" not in root.lower():
            # Keep title short; full why lives in notes
            pass

        notes_payload = {
            "rca_mode": mode,
            "confidence_percent": result.confidence,
            "why_root_cause": result.why_root_cause,
            "affected_components": result.affected_components,
            "evidence": result.evidence,
            "suggested_actions": result.suggested_actions,
            "runbook_suggestion": result.runbook_suggestion,
            # Trace experience — primary deep-link for Incident UI
            "primary_trace_id": primary_trace_id or result.primary_trace_id,
            "grafana_trace_url": trace_url,
            "grafana_service_traces_url": obs_links.get("service_traces_url"),
            "grafana_logs_url": obs_links.get("service_logs_url"),
            "related_traces": (related_traces or [])[:8],
        }
        if extra_notes:
            notes_payload["meta"] = extra_notes

        # Append why to description-facing root_cause for operators scanning tickets
        root_out = result.root_cause
        if result.why_root_cause:
            root_out = f"{result.root_cause} | Why: {result.why_root_cause[:400]}"

        body = {
            "root_cause": root_out[:2000],
            "rca_confidence": max(0.0, min(1.0, result.confidence / 100.0)),
            "remediation_notes": json.dumps(notes_payload, ensure_ascii=False)[:8000],
            "status": "investigating",
        }
        r = self._http.patch(
            f"{self.base_url}/incidents/{incident_id}",
            json=body,
        )
        if r.status_code == 404:
            raise LookupError(f"incident not found: {incident_id}")
        r.raise_for_status()
        logger.info(
            "persisted RCA incident=%s confidence=%s mode=%s",
            incident_id,
            result.confidence,
            mode,
        )
        return r.json()
