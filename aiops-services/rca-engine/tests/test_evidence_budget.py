from __future__ import annotations

import json

from app.models import EvidencePack


def test_prompt_budget_is_valid_json_and_keeps_first_evidence():
    pack = EvidencePack(
        incident_id="inc-large",
        service_name="checkout-service",
        window_minutes=10,
        window_start_iso="2026-08-08T00:00:00Z",
        window_end_iso="2026-08-08T00:10:00Z",
        incident={
            "id": "inc-large",
            "metric_name": "http_error_rate",
            "metric_value": 0.8,
            "description": "x" * 5000,
        },
        error_logs=[
            {"line": f"important error {i} " + ("y" * 400), "trace_id": str(i)}
            for i in range(40)
        ],
        traces=[
            {"trace_id": str(i), "error": True, "detail": "z" * 500}
            for i in range(15)
        ],
        primary_trace_id="0",
        sources_ok={"loki": True, "tempo": True},
    )

    block = pack.to_prompt_block(max_chars=1800)
    decoded = json.loads(block)

    assert len(block) <= 1800
    assert decoded["evidence_budget"]["truncated"] is True
    assert decoded["evidence_budget"]["original_counts"]["error_logs"] == 40
    if decoded.get("error_logs"):
        assert decoded["error_logs"][0]["trace_id"] == "0"


def test_tiny_prompt_budget_still_returns_valid_json():
    pack = EvidencePack(
        incident_id="i",
        service_name="svc",
        window_minutes=1,
        window_start_iso="a",
        window_end_iso="b",
        incident={"description": "large" * 1000},
    )
    block = pack.to_prompt_block(max_chars=20)
    assert len(block) <= 20
    assert isinstance(json.loads(block), dict)
