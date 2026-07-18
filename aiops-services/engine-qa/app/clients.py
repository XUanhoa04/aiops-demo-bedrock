"""HTTP clients for pipeline snapshot + optional feedback sync."""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class PipelineClients:
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=httpx.Timeout(20.0, connect=2.0))

    def close(self) -> None:
        self._http.close()

    def probe(self) -> dict[str, bool]:
        return {
            "incident_manager": self._ok(f"{settings.incident_manager_url}/health"),
            "anomaly_detector": self._ok(f"{settings.anomaly_detector_url}/health"),
            "decision_engine": self._ok(f"{settings.decision_engine_url}/health"),
            "rca_engine": self._ok(f"{settings.rca_engine_url}/health"),
            "feedback": self._ok(f"{settings.feedback_url}/health"),
        }

    def _ok(self, url: str) -> bool:
        try:
            r = self._http.get(url, timeout=2.0)
            return r.status_code < 500
        except Exception:
            return False

    def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        try:
            r = self._http.get(
                f"{settings.incident_manager_url.rstrip('/')}/incidents/{incident_id}"
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("get incident: %s", exc)
            return None

    def list_incidents(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            r = self._http.get(
                f"{settings.incident_manager_url.rstrip('/')}/incidents",
                params={"limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("list incidents: %s", exc)
            return []

    def list_decisions(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            r = self._http.get(
                f"{settings.decision_engine_url.rstrip('/')}/decisions",
                params={"limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("list decisions: %s", exc)
            return []

    def list_anomalies(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            r = self._http.get(
                f"{settings.anomaly_detector_url.rstrip('/')}/anomalies",
                params={"limit": limit},
            )
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("list anomalies: %s", exc)
            return []

    def patch_incident_feedback(self, incident_id: str, text: str) -> bool:
        try:
            r = self._http.patch(
                f"{settings.incident_manager_url.rstrip('/')}/incidents/{incident_id}",
                json={"human_feedback": text},
            )
            return r.status_code < 400
        except Exception as exc:
            logger.warning("patch incident feedback: %s", exc)
            return False

    def push_feedback_collector(
        self,
        *,
        incident_id: str,
        anomaly_correct: Optional[bool],
        rca_useful: Optional[bool],
        comment: str,
        reviewer: str,
        corrected_root_cause: Optional[str],
    ) -> bool:
        """Best-effort dual-write so feedback-collector metrics stay aligned."""
        if not settings.sync_feedback_collector:
            return False
        body = {
            "incident_id": incident_id,
            "anomaly_correct": anomaly_correct,
            "rca_useful": rca_useful,
            "action_effective": None,
            "comment": f"[engine-qa] {comment}".strip(),
            "reviewer": reviewer,
            "corrected_root_cause": corrected_root_cause,
        }
        # feedback API requires at least one thumb or comment
        if (
            anomaly_correct is None
            and rca_useful is None
            and not (comment or "").strip()
            and not corrected_root_cause
        ):
            return False
        try:
            r = self._http.post(
                f"{settings.feedback_url.rstrip('/')}/feedback",
                json=body,
            )
            if r.status_code >= 400:
                logger.debug("feedback sync HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            logger.debug("feedback sync failed: %s", exc)
            return False
