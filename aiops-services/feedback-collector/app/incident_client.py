"""HTTP helpers for Incident Manager."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class IncidentClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.incident_manager_url).rstrip("/")
        self._http = httpx.Client(timeout=httpx.Timeout(10.0, connect=3.0))

    def close(self) -> None:
        self._http.close()

    def healthy(self) -> bool:
        try:
            return self._http.get(f"{self.base_url}/health").status_code == 200
        except Exception:
            return False

    def list_incidents(self, limit: int = 30) -> list[dict[str, Any]]:
        r = self._http.get(
            f"{self.base_url}/incidents",
            params={"limit": limit},
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    def get_incident(self, incident_id: str) -> dict[str, Any]:
        r = self._http.get(f"{self.base_url}/incidents/{incident_id}")
        if r.status_code == 404:
            raise LookupError(incident_id)
        r.raise_for_status()
        return r.json()

    def apply_feedback(
        self,
        incident_id: str,
        *,
        human_feedback: str,
        mark_false_positive: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"human_feedback": human_feedback[:4000]}
        if mark_false_positive:
            body["status"] = "false_positive"
        else:
            # Keep current status unless open → acknowledge review happened
            body["status"] = "resolved"
        r = self._http.patch(
            f"{self.base_url}/incidents/{incident_id}",
            json=body,
        )
        if r.status_code == 404:
            raise LookupError(incident_id)
        r.raise_for_status()
        return r.json()
