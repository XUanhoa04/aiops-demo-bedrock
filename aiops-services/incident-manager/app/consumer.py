"""Redis anomaly consumer → create / correlate incidents."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiops_shared.models import AnomalyEvent, Incident
from aiops_shared.redis_client import dequeue, enqueue, get_redis, ping

from app.config import settings
from app.db import IncidentRepository, incident_from_anomaly
from app.decision_client import DecisionClient
from app.prom_metrics import ERRORS_TOTAL, record_correlated, record_created, set_open_incidents
from app.rca_client import RCAClient

logger = logging.getLogger(__name__)


class AnomalyConsumer:
    def __init__(
        self,
        repo: IncidentRepository,
        rca: Optional[RCAClient] = None,
        decision: Optional[DecisionClient] = None,
    ) -> None:
        self.repo = repo
        self.rca = rca or RCAClient()
        self.decision = decision or DecisionClient()
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.processed = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        self._stop.clear()
        self._refresh_open_gauge()
        self._task = asyncio.create_task(self._run(), name="anomaly-consumer")
        logger.info(
            "consumer started queue=%s", settings.redis_queue_anomalies
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        logger.info("consumer stopped processed=%s", self.processed)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(
                    dequeue,
                    self.redis,
                    settings.redis_queue_anomalies,
                    2,
                )
                if raw is None:
                    continue
                await asyncio.to_thread(self._handle_payload, raw)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                ERRORS_TOTAL.labels(stage="consumer").inc()
                logger.exception("consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle_payload(self, raw: str) -> Incident:
        anomaly = AnomalyEvent.from_redis_json(raw)
        return self.handle_anomaly(anomaly, source="redis")

    def handle_anomaly(
        self,
        anomaly: AnomalyEvent,
        *,
        source: str = "redis",
    ) -> Incident:
        """Create or correlate an incident from an anomaly event."""
        existing = self.repo.find_open_correlated(
            service_name=anomaly.service_name,
            metric_name=anomaly.metric_name,
            window_minutes=settings.correlation_window_minutes,
        )
        if existing:
            # Update metric snapshot; keep single ticket (noise reduction)
            existing.metric_value = anomaly.metric_value
            existing.context = {
                **existing.context,
                "last_anomaly_id": anomaly.id,
                "occurrence_count": int(existing.context.get("occurrence_count", 1)) + 1,
                # anomaly_details keeps the latest detector payload for UI/RCA
                "anomaly_details": {
                    "anomaly_id": anomaly.id,
                    "metric_name": anomaly.metric_name,
                    "metric_value": anomaly.metric_value,
                    "threshold": anomaly.threshold,
                    "detector": anomaly.detector,
                    "message": anomaly.message,
                    "labels": anomaly.labels,
                    "context": anomaly.context,
                    "detected_at": anomaly.detected_at.isoformat(),
                },
            }
            existing.description = (
                f"{existing.description}\n---\n{anomaly.message}".strip()
            )
            if anomaly.severity.value == "critical":
                existing.severity = anomaly.severity
            self.repo.update(existing)
            record_correlated(anomaly.service_name)
            self._refresh_open_gauge()
            logger.info(
                "correlated anomaly=%s → incident=%s count=%s",
                anomaly.id,
                existing.id,
                existing.context.get("occurrence_count"),
            )
            self.processed += 1
            return existing

        incident = incident_from_anomaly(anomaly)
        self.repo.insert(incident)
        record_created(source=source, severity=incident.severity.value, service=incident.service_name)
        self._refresh_open_gauge()

        self.fanout_new_incident(incident, anomaly=anomaly)

        logger.warning(
            "incident created id=%s title=%s severity=%s source=%s",
            incident.id,
            incident.title,
            incident.severity.value,
            source,
        )
        self.processed += 1
        return incident

    def fanout_new_incident(
        self,
        incident: Incident,
        anomaly: Optional[AnomalyEvent] = None,
    ) -> None:
        """Push new tickets to Redis + Decision Engine + RCA (best-effort)."""
        if settings.enable_redis_incident_fanout:
            try:
                enqueue(
                    self.redis,
                    settings.redis_queue_incidents,
                    incident.model_dump_json(),
                )
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="redis").inc()
                logger.warning("incident fan-out failed: %s", exc)

        # Policy routing first (cost-aware: may escalate without LLM)
        if self.decision.enabled:
            self.decision.push(incident, anomaly=anomaly)

        # RCA hook (async); Decision Engine may also call RCA on medium band
        if self.rca.enabled:
            self.rca.push_incident(incident)

    def _refresh_open_gauge(self) -> None:
        try:
            set_open_incidents(self.repo.count_open())
        except Exception as exc:
            ERRORS_TOTAL.labels(stage="metrics").inc()
            logger.warning("open_incidents gauge refresh failed: %s", exc)

    def status(self) -> dict:
        return {
            "processed": self.processed,
            "redis_ok": ping(self.redis),
            "last_error": self.last_error,
            "queue": settings.redis_queue_anomalies,
            "rca": self.rca.status(),
            "decision": self.decision.status(),
        }
