"""HTTP client for Incident Manager (+ optional RCA)."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class IncidentClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.incident_manager_url).rstrip("/")
        self.rca_url = settings.rca_engine_url.rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(12.0, connect=3.0))

    def close(self) -> None:
        self._http.close()

    def healthy(self) -> bool:
        try:
            return self._http.get(f"{self.base_url}/health").status_code == 200
        except Exception:
            return False

    def list_incidents(
        self,
        status: Optional[str] = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        r = self._http.get(f"{self.base_url}/incidents", params=params)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        r = self._http.get(f"{self.base_url}/incidents/{incident_id}")
        if r.status_code == 404:
            raise LookupError(incident_id)
        r.raise_for_status()
        return r.json()

    def patch_incident(self, incident_id: str, body: dict[str, Any]) -> dict[str, Any]:
        r = self._http.patch(f"{self.base_url}/incidents/{incident_id}", json=body)
        if r.status_code == 404:
            raise LookupError(incident_id)
        r.raise_for_status()
        return r.json()

    def mark_false_positive(self, incident_id: str, note: str) -> dict[str, Any]:
        return self.patch_incident(
            incident_id,
            {
                "status": "false_positive",
                "human_feedback": note,
            },
        )

    def mark_remediating(self, incident_id: str, notes: str) -> dict[str, Any]:
        return self.patch_incident(
            incident_id,
            {
                "status": "remediating",
                "remediation_notes": notes[:4000],
            },
        )

    def extract_rca(self, incident: dict[str, Any]) -> dict[str, Any]:
        """Pull structured RCA from incident fields + remediation_notes JSON."""
        notes_raw = incident.get("remediation_notes") or ""
        notes: dict[str, Any] = {}
        if isinstance(notes_raw, str) and notes_raw.strip().startswith("{"):
            try:
                notes = json.loads(notes_raw)
            except json.JSONDecodeError:
                notes = {"raw": notes_raw}
        elif isinstance(notes_raw, dict):
            notes = notes_raw

        suggested = notes.get("suggested_actions") or []
        if not isinstance(suggested, list):
            suggested = [str(suggested)]

        return {
            "root_cause": incident.get("root_cause"),
            "rca_confidence": incident.get("rca_confidence"),
            "suggested_actions": [str(s) for s in suggested],
            "evidence": notes.get("evidence") or [],
            "affected_components": notes.get("affected_components") or [],
            "runbook_suggestion": notes.get("runbook_suggestion") or "",
            "rca_mode": notes.get("rca_mode"),
            "confidence_percent": notes.get("confidence_percent"),
        }
