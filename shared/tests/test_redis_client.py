from __future__ import annotations

import json
from unittest.mock import MagicMock

from aiops_shared.redis_client import (
    acknowledge,
    recover_inflight,
    reserve,
    retry_or_dead_letter,
)


def test_reserve_and_ack_use_processing_list() -> None:
    client = MagicMock()
    client.brpoplpush.return_value = '{"id":"a1"}'
    client.lrem.return_value = 1

    payload = reserve(client, "events", "events:processing", 2)
    assert payload == '{"id":"a1"}'
    assert acknowledge(client, "events:processing", payload)
    client.brpoplpush.assert_called_once_with(
        "events", "events:processing", timeout=2
    )


def test_failed_payload_retries_then_enters_dlq() -> None:
    client = MagicMock()
    client.incr.side_effect = [1, 2]
    payload = '{"id":"bad"}'

    first = retry_or_dead_letter(
        client,
        queue="events",
        processing_queue="events:processing",
        dead_letter_queue="events:dlq",
        payload=payload,
        error="temporary",
        max_retries=1,
    )
    second = retry_or_dead_letter(
        client,
        queue="events",
        processing_queue="events:processing",
        dead_letter_queue="events:dlq",
        payload=payload,
        error="poison",
        max_retries=1,
    )

    assert first == "retried"
    assert second == "dead_lettered"
    envelope = json.loads(client.lpush.call_args.args[1])
    assert envelope["payload"] == payload
    assert envelope["attempts"] == 2


def test_recover_inflight_is_bounded() -> None:
    client = MagicMock()
    client.rpoplpush.side_effect = ["a", "b", None]
    assert recover_inflight(
        client, queue="events", processing_queue="events:processing", limit=10
    ) == 2
