"""
Best-effort hand-off to the RCA Engine.

* When RCA_ENGINE_URL is empty → no-op.
* When set → POST /rca/analyze with the incident id.
* Failures are logged + counted; they never block ticket creation.

Production notes
----------------
Replace fire-and-forget HTTP with a durable outbox / Redis Streams consumer
group so RCA retries survive process restarts.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from aiops_shared.models import Incident

from app.config import settings
from app.prom_metrics import ERRORS_TOTAL

logger = logging.getLogger(__name__)


class RCAClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url if base_url is not None else settings.rca_engine_url).rstrip(
            "/"
        )
        self._http = httpx.Client(timeout=settings.rca_timeout_sec)
        self.pushed = 0
        self.last_error: Optional[str] = None

    def close(self) -> None:
        self._http.close()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def push_incident(self, incident: Incident) -> dict[str, Any]:
        """Notify RCA Engine about a newly created incident. Best-effort."""
        if not self.enabled:
            return {"ok": False, "skipped": True, "reason": "rca_engine_url empty"}

        url = f"{self.base_url}/rca/analyze"
        try:
            resp = self._http.post(url, json={"incident_id": incident.id})
            if resp.status_code >= 400:
                ERRORS_TOTAL.labels(stage="rca").inc()
                self.last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.warning(
                    "rca push failed incident=%s status=%s body=%s",
                    incident.id,
                    resp.status_code,
                    resp.text[:200],
                )
                return {"ok": False, "status_code": resp.status_code, "body": resp.text[:300]}

            self.pushed += 1
            self.last_error = None
            body: Any
            try:
                body = resp.json()
            except Exception:
                body = resp.text[:300]
            logger.info("rca push ok incident=%s", incident.id)
            return {"ok": True, "status_code": resp.status_code, "response": body}
        except Exception as exc:
            ERRORS_TOTAL.labels(stage="rca").inc()
            self.last_error = str(exc)
            logger.warning("rca push error incident=%s: %s", incident.id, exc)
            return {"ok": False, "error": str(exc)}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "url": self.base_url or None,
            "pushed": self.pushed,
            "last_error": self.last_error,
        }
