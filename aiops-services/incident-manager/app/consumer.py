"""Redis anomaly consumer → create / correlate incidents."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiops_shared.models import AnomalyEvent, Incident
from aiops_shared.redis_client import dequeue, enqueue, get_redis, ping

from app.config import settings
from app.db import IncidentRepository, incident_from_anomaly

logger = logging.getLogger(__name__)


class AnomalyConsumer:
    def __init__(self, repo: IncidentRepository) -> None:
        self.repo = repo
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.processed = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        self._stop.clear()
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
                logger.exception("consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle_payload(self, raw: str) -> Incident:
        anomaly = AnomalyEvent.from_redis_json(raw)
        return self.handle_anomaly(anomaly)

    def handle_anomaly(self, anomaly: AnomalyEvent) -> Incident:
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
            }
            existing.description = (
                f"{existing.description}\n---\n{anomaly.message}".strip()
            )
            if anomaly.severity.value == "critical":
                existing.severity = anomaly.severity
            self.repo.update(existing)
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
        # Optional fan-out for Day-2 RCA / chatops listeners
        try:
            enqueue(
                self.redis,
                settings.redis_queue_incidents,
                incident.model_dump_json(),
            )
        except Exception as exc:
            logger.warning("incident fan-out failed: %s", exc)

        logger.warning(
            "incident created id=%s title=%s severity=%s",
            incident.id,
            incident.title,
            incident.severity.value,
        )
        self.processed += 1
        return incident

    def status(self) -> dict:
        return {
            "processed": self.processed,
            "redis_ok": ping(self.redis),
            "last_error": self.last_error,
            "queue": settings.redis_queue_anomalies,
        }
