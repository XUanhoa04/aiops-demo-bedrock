"""Redis poller: consume new incidents from incident-manager fan-out queue."""

from __future__ import annotations

import asyncio
import json
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
from app.engine import RCAEngine

logger = logging.getLogger(__name__)


class IncidentConsumer:
    def __init__(self, engine: RCAEngine) -> None:
        self.engine = engine
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.processed = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if not settings.enable_redis_poll:
            logger.info("redis incident poll disabled")
            return
        self._stop.clear()
        recovered = await asyncio.to_thread(
            recover_inflight,
            self.redis,
            queue=settings.redis_queue_incidents,
            processing_queue=settings.redis_queue_incidents_processing,
        )
        self._task = asyncio.create_task(self._run(), name="rca-incident-consumer")
        logger.info(
            "RCA consumer started queue=%s recovered=%s",
            settings.redis_queue_incidents,
            recovered,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        logger.info("RCA consumer stopped processed=%s", self.processed)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(
                    reserve,
                    self.redis,
                    settings.redis_queue_incidents,
                    settings.redis_queue_incidents_processing,
                    2,
                )
                if raw is None:
                    continue
                try:
                    await asyncio.to_thread(self._handle, raw)
                except Exception as exc:
                    disposition = await asyncio.to_thread(
                        retry_or_dead_letter,
                        self.redis,
                        queue=settings.redis_queue_incidents,
                        processing_queue=settings.redis_queue_incidents_processing,
                        dead_letter_queue=settings.redis_queue_incidents_dlq,
                        payload=raw,
                        error=str(exc),
                        max_retries=settings.queue_max_retries,
                    )
                    logger.exception(
                        "RCA processing failed disposition=%s: %s",
                        disposition,
                        exc,
                    )
                    raise
                await asyncio.to_thread(
                    acknowledge,
                    self.redis,
                    settings.redis_queue_incidents_processing,
                    raw,
                )
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("RCA consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid incident JSON on queue") from exc
        incident_id = payload.get("id")
        if not incident_id:
            raise ValueError("queue payload missing id")
        logger.info("RCA trigger from redis incident=%s", incident_id)
        resp = self.engine.analyze_incident(str(incident_id), persist=True, force=False)
        self.processed += 1
        logger.info(
            "RCA redis result incident=%s status=%s mode=%s",
            incident_id,
            resp.status,
            resp.mode,
        )

    def status(self) -> dict:
        return {
            "enabled": settings.enable_redis_poll,
            "processed": self.processed,
            "redis_ok": ping(self.redis) if settings.enable_redis_poll else None,
            "last_error": self.last_error,
            "queue": settings.redis_queue_incidents,
            "processing_queue": settings.redis_queue_incidents_processing,
            "dead_letter_queue": settings.redis_queue_incidents_dlq,
        }
