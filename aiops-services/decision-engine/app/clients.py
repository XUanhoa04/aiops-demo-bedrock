"""
HTTP clients to Incident Manager, RCA Engine, Remediation, Anomaly Detector.

All failures are non-fatal at the call site — Decision Engine logs and degrades
(e.g. escalate if RCA is down on medium path).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class ServiceClients:
    def __init__(self) -> None:
        self._http = httpx.Client(timeout=httpx.Timeout(45.0, connect=3.0))

    def close(self) -> None:
        self._http.close()

    def probe(self) -> dict[str, bool]:
        return {
            "incident_manager": self._ok(f"{settings.incident_manager_url}/health"),
            "rca_engine": self._ok(f"{settings.rca_engine_url}/health"),
            "remediation": self._ok(f"{settings.remediation_url}/health"),
            "anomaly_detector": self._ok(f"{settings.anomaly_detector_url}/health"),
        }

    def _ok(self, url: str) -> bool:
        try:
            r = self._http.get(url, timeout=2.0)
            return r.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Anomaly detector — re-score / enrich
    # ------------------------------------------------------------------

    def refresh_confidence(self, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        POST /score on anomaly-detector to re-gather context + recompute confidence.
        """
        url = f"{settings.anomaly_detector_url.rstrip('/')}/score"
        try:
            r = self._http.post(url, json=body)
            if r.status_code >= 400:
                logger.warning("score refresh HTTP %s: %s", r.status_code, r.text[:200])
                return None
            return r.json()
        except Exception as exc:
            logger.warning("score refresh failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # RCA Engine — MEDIUM path only
    # ------------------------------------------------------------------

    def run_rca(
        self,
        incident_id: str,
        *,
        wait: bool = True,
        force: bool = True,
        persist: bool = True,
    ) -> Optional[dict[str, Any]]:
        url = f"{settings.rca_engine_url.rstrip('/')}/rca/analyze"
        payload = {
            "incident_id": incident_id,
            "wait": wait,
            "force": force,
            "persist": persist,
        }
        try:
            r = self._http.post(url, json=payload)
            if r.status_code >= 400:
                logger.error("RCA HTTP %s: %s", r.status_code, r.text[:300])
                return None
            return r.json()
        except Exception as exc:
            logger.error("RCA call failed: %s", exc)
            return None

    def analyze_incident_direct(
        self, incident_id: str, *, force: bool = True, persist: bool = True
    ) -> Optional[dict[str, Any]]:
        url = (
            f"{settings.rca_engine_url.rstrip('/')}/analyze-incident/{incident_id}"
            f"?force={str(force).lower()}&persist={str(persist).lower()}"
        )
        try:
            r = self._http.post(url)
            if r.status_code >= 400:
                logger.error("analyze-incident HTTP %s: %s", r.status_code, r.text[:300])
                return None
            return r.json()
        except Exception as exc:
            logger.error("analyze-incident failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Remediation — HIGH gated path
    # ------------------------------------------------------------------

    def propose_remediation(
        self,
        incident_id: str,
        actions: list[str],
        *,
        auto_execute_low_risk: bool = False,
    ) -> Optional[list[dict[str, Any]]]:
        url = f"{settings.remediation_url.rstrip('/')}/remediate/propose"
        payload = {
            "incident_id": incident_id,
            "actions": actions,
            "auto_execute_low_risk": auto_execute_low_risk,
        }
        try:
            r = self._http.post(url, json=payload)
            if r.status_code >= 400:
                logger.error("remediate/propose HTTP %s: %s", r.status_code, r.text[:300])
                return None
            data = r.json()
            return data if isinstance(data, list) else [data]
        except Exception as exc:
            logger.error("remediate/propose failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Incident Manager
    # ------------------------------------------------------------------

    def find_incident_by_anomaly(self, anomaly_id: str) -> Optional[dict[str, Any]]:
        """Best-effort: list recent incidents and match source_anomaly_id."""
        url = f"{settings.incident_manager_url.rstrip('/')}/incidents"
        try:
            r = self._http.get(url, params={"limit": 30})
            if r.status_code >= 400:
                return None
            items = r.json()
            if not isinstance(items, list):
                return None
            for inc in items:
                if str(inc.get("source_anomaly_id") or "") == anomaly_id:
                    return inc
            return None
        except Exception as exc:
            logger.debug("find incident failed: %s", exc)
            return None

    def get_incident(self, incident_id: str) -> Optional[dict[str, Any]]:
        url = f"{settings.incident_manager_url.rstrip('/')}/incidents/{incident_id}"
        try:
            r = self._http.get(url)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("get incident failed: %s", exc)
            return None

    def create_incident_from_anomaly(self, anomaly_payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        url = f"{settings.incident_manager_url.rstrip('/')}/incidents/from-anomaly"
        try:
            r = self._http.post(url, json=anomaly_payload)
            if r.status_code >= 400:
                logger.warning(
                    "from-anomaly HTTP %s: %s", r.status_code, r.text[:200]
                )
                return None
            return r.json()
        except Exception as exc:
            logger.warning("from-anomaly failed: %s", exc)
            return None

    def patch_incident(
        self,
        incident_id: str,
        *,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        description: Optional[str] = None,
        remediation_notes: Optional[str] = None,
        root_cause: Optional[str] = None,
        rca_confidence: Optional[float] = None,
        context_merge: Optional[dict[str, Any]] = None,
    ) -> bool:
        """
        PATCH incident. Incident Manager may not accept arbitrary context merge;
        we put decision trail into description / remediation_notes as fallback.
        """
        url = f"{settings.incident_manager_url.rstrip('/')}/incidents/{incident_id}"
        body: dict[str, Any] = {}
        if status:
            body["status"] = status
        if severity:
            body["severity"] = severity
        if description is not None:
            body["description"] = description
        if remediation_notes is not None:
            body["remediation_notes"] = remediation_notes
        if root_cause is not None:
            body["root_cause"] = root_cause
        if rca_confidence is not None:
            # IM expects 0–1 for rca_confidence
            body["rca_confidence"] = (
                rca_confidence / 100.0 if rca_confidence > 1.0 else rca_confidence
            )
        if not body:
            return False
        try:
            r = self._http.patch(url, json=body)
            if r.status_code >= 400:
                logger.warning("patch incident HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except Exception as exc:
            logger.warning("patch incident failed: %s", exc)
            return False
