"""Redis poller: consume new incidents from incident-manager fan-out queue."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from aiops_shared.redis_client import dequeue, get_redis, ping

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
        self._task = asyncio.create_task(self._run(), name="rca-incident-consumer")
        logger.info("RCA consumer started queue=%s", settings.redis_queue_incidents)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        logger.info("RCA consumer stopped processed=%s", self.processed)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(
                    dequeue,
                    self.redis,
                    settings.redis_queue_incidents,
                    2,
                )
                if raw is None:
                    continue
                await asyncio.to_thread(self._handle, raw)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("RCA consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle(self, raw: str) -> None:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("invalid incident JSON on queue")
            return
        incident_id = payload.get("id")
        if not incident_id:
            logger.warning("queue payload missing id")
            return
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
        }
