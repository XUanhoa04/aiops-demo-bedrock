"""Durable checkpoint adapter for detector baselines."""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from aiops_shared.redis_client import get_redis

from app.config import settings

logger = logging.getLogger(__name__)


class DetectorStateStore:
    """Persist checkpoints without making Redis failure fatal to detection."""

    def __init__(self, redis_client: Any = None) -> None:
        self.redis = redis_client or get_redis(settings.redis_url)
        self.last_error: Optional[str] = None
        self.last_restore_counts: dict[str, int] = {}

    def restore_into(self, detector: Any) -> bool:
        if not settings.enable_state_persistence:
            return False
        try:
            raw = self.redis.get(settings.detector_state_key)
            if not raw:
                self.last_error = None
                return False
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            self.last_restore_counts = detector.restore(json.loads(raw))
            self.last_error = None
            logger.info("detector checkpoint restored counts=%s", self.last_restore_counts)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("detector checkpoint restore failed: %s", exc)
            return False

    def save(self, detector: Any) -> bool:
        if not settings.enable_state_persistence:
            return False
        try:
            payload = json.dumps(
                detector.snapshot(), separators=(",", ":"), allow_nan=False
            )
            self.redis.set(settings.detector_state_key, payload)
            self.last_error = None
            return True
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning("detector checkpoint save failed: %s", exc)
            return False
