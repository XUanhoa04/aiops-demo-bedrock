"""
Redis helpers for anomaly / incident queues.

Production choice: Redis LIST as a simple work queue (LPUSH/BRPOP).
Real systems often use Redis Streams, SQS, or Kafka for consumer groups & replay.
"""

from __future__ import annotations

import logging
import os
import hashlib
import json
import time
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


def reserve(
    client: redis.Redis,
    queue: str,
    processing_queue: str,
    timeout_sec: int = 5,
) -> Optional[str]:
    """Atomically move one item to an in-flight list before processing.

    Unlike ``BRPOP``, ``BRPOPLPUSH`` keeps the payload recoverable if the
    worker exits between receipt and persistence. Call :func:`acknowledge`
    only after all side effects for the item have completed.
    """
    payload = client.brpoplpush(queue, processing_queue, timeout=timeout_sec)
    return str(payload) if payload is not None else None


def _retry_key(processing_queue: str, payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()
    return f"{processing_queue}:retry:{digest}"


def acknowledge(client: redis.Redis, processing_queue: str, payload: str) -> bool:
    """ACK a reserved payload and clear its retry counter."""
    removed = int(client.lrem(processing_queue, 1, payload))
    client.delete(_retry_key(processing_queue, payload))
    return removed > 0


def retry_or_dead_letter(
    client: redis.Redis,
    *,
    queue: str,
    processing_queue: str,
    dead_letter_queue: str,
    payload: str,
    error: str,
    max_retries: int = 3,
) -> str:
    """Requeue a failed reservation or move it to a bounded DLQ envelope."""
    key = _retry_key(processing_queue, payload)
    attempts = int(client.incr(key))
    client.expire(key, 86400)
    client.lrem(processing_queue, 1, payload)
    if attempts <= max(0, max_retries):
        client.rpush(queue, payload)
        return "retried"

    envelope = json.dumps(
        {
            "payload": payload,
            "error": str(error)[:1000],
            "attempts": attempts,
            "failed_at_unix": int(time.time()),
        },
        ensure_ascii=False,
    )
    client.lpush(dead_letter_queue, envelope)
    client.delete(key)
    return "dead_lettered"


def recover_inflight(
    client: redis.Redis,
    *,
    queue: str,
    processing_queue: str,
    limit: int = 1000,
) -> int:
    """Return reservations left by a previous worker process to the source."""
    recovered = 0
    for _ in range(max(0, limit)):
        payload = client.rpoplpush(processing_queue, queue)
        if payload is None:
            break
        recovered += 1
    if recovered:
        logger.warning(
            "recovered in-flight messages source=%s processing=%s count=%s",
            queue,
            processing_queue,
            recovered,
        )
    return recovered


def ping(client: redis.Redis) -> bool:
    try:
        return bool(client.ping())
    except redis.RedisError as exc:
        logger.warning("redis ping failed: %s", exc)
        return False
