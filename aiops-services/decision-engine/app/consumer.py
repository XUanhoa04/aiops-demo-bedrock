"""
Optional Redis consumer for `aiops:decisions` payloads.

Incident Manager already consumes `aiops:anomalies`. Decision Engine uses a
*separate* queue to avoid double-pop races. Producers:

  * anomaly-detector dual-publish (optional future)
  * scripts / console POST that also LPUSH
  * `POST /decide/from-anomaly` after IM creates the ticket
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiops_shared.redis_client import (
    acknowledge,
    get_redis,
    ping,
    recover_inflight,
    reserve,
    retry_or_dead_letter,
)

from app.config import settings
from app.engine import DecisionEngine
from app.models import AnomalyEventIn, anomaly_event_to_request

logger = logging.getLogger(__name__)


class DecisionConsumer:
    def __init__(self, engine: DecisionEngine) -> None:
        self.engine = engine
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.consumed = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if not settings.enable_redis_consumer:
            logger.info("decision redis consumer disabled")
            return
        self._stop.clear()
        recovered = await asyncio.to_thread(
            recover_inflight,
            self.redis,
            queue=settings.redis_queue_decisions,
            processing_queue=settings.redis_queue_decisions_processing,
        )
        self._task = asyncio.create_task(self._run(), name="decision-consumer")
        logger.info(
            "decision consumer started queue=%s recovered=%s",
            settings.redis_queue_decisions,
            recovered,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(
                    reserve,
                    self.redis,
                    settings.redis_queue_decisions,
                    settings.redis_queue_decisions_processing,
                    3,
                )
                if raw is None:
                    continue
                try:
                    await asyncio.to_thread(self._handle, raw)
                except Exception as exc:
                    disposition = await asyncio.to_thread(
                        retry_or_dead_letter,
                        self.redis,
                        queue=settings.redis_queue_decisions,
                        processing_queue=settings.redis_queue_decisions_processing,
                        dead_letter_queue=settings.redis_queue_decisions_dlq,
                        payload=raw,
                        error=str(exc),
                        max_retries=settings.queue_max_retries,
                    )
                    logger.exception(
                        "decision processing failed disposition=%s: %s",
                        disposition,
                        exc,
                    )
                    raise
                await asyncio.to_thread(
                    acknowledge,
                    self.redis,
                    settings.redis_queue_decisions_processing,
                    raw,
                )
                self.consumed += 1
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("decision consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle(self, raw: str) -> None:
        import time

        event = AnomalyEventIn.model_validate_json(raw)
        req = anomaly_event_to_request(event)
        # IM may still be creating the ticket (parallel queue) — retry briefly
        if event.id:
            for attempt in range(5):
                inc = self.engine.clients.find_incident_by_anomaly(event.id)
                if inc:
                    req.incident_id = str(inc.get("id"))
                    break
                time.sleep(0.4 * (attempt + 1))
            if not req.incident_id:
                logger.info(
                    "no incident yet for anomaly=%s — deciding without side-effect targets",
                    event.id,
                )
        decision = self.engine.decide(req)
        logger.warning(
            "consumer decided id=%s action=%s conf=%.1f incident=%s",
            decision.id,
            decision.action.value,
            decision.confidence_score,
            decision.incident_id,
        )

    def status(self) -> dict:
        return {
            "enabled": settings.enable_redis_consumer,
            "queue": settings.redis_queue_decisions,
            "processing_queue": settings.redis_queue_decisions_processing,
            "dead_letter_queue": settings.redis_queue_decisions_dlq,
            "redis_ok": ping(self.redis),
            "consumed": self.consumed,
            "last_error": self.last_error,
        }
