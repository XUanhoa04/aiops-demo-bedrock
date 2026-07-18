"""
Redis helpers for anomaly / incident queues.

Production choice: Redis LIST as a simple work queue (LPUSH/BRPOP).
Real systems often use Redis Streams, SQS, or Kafka for consumer groups & replay.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis

logger = logging.getLogger(__name__)


def get_redis(url: Optional[str] = None) -> redis.Redis:
    url = url or os.getenv("REDIS_URL", "redis://redis:6379/0")
    # decode_responses=True → str payloads, matches Pydantic JSON strings
    client = redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )
    return client


def enqueue(client: redis.Redis, queue: str, payload: str) -> int:
    """LPUSH for FIFO when paired with BRPOP (producer left, consumer right)."""
    length = client.lpush(queue, payload)
    logger.debug("enqueued queue=%s len=%s", queue, length)
    return int(length)


def dequeue(
    client: redis.Redis,
    queue: str,
    timeout_sec: int = 5,
) -> Optional[str]:
    """
    BRPOP blocks up to timeout_sec. Returns payload or None on timeout.
    Blocking pop is efficient for demo workers (no busy-loop CPU).
    """
    result = client.brpop(queue, timeout=timeout_sec)
    if result is None:
        return None
    _queue_name, payload = result
    return payload


def ping(client: redis.Redis) -> bool:
    try:
        return bool(client.ping())
    except redis.RedisError as exc:
        logger.warning("redis ping failed: %s", exc)
        return False
