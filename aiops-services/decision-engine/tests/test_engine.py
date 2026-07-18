"""Engine tests with skip_side_effects (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))

from app.decision_table import DecisionAction  # noqa: E402
from app.engine import DecisionEngine  # noqa: E402
from app.models import DecideRequest  # noqa: E402


def test_engine_high_pattern_gated():
    eng = DecisionEngine()
    d = eng.decide(
        DecideRequest(
            service_name="checkout-service",
            metric_name="http_error_rate",
            metric_value=0.5,
            confidence_score=90,
            confidence_breakdown={"metrics": 36, "traces": 24, "logs": 16, "events": 5},
            missing_context=[],
            context_completeness=1.0,
            explanation="error rate cao hơn 3.2 sigma so với EWMA baseline",
            skip_side_effects=True,
        )
    )
    eng.close()
    assert d.action == DecisionAction.AUTO_REMEDIATE_GATED
    assert d.known_pattern_id is not None
    assert d.decision_trace
    assert "confidence" in d.reason.lower() or d.confidence_score == 90


def test_engine_low_escalates():
    eng = DecisionEngine()
    d = eng.decide(
        DecideRequest(
            service_name="payment-service",
            metric_name="http_latency_p95_seconds",
            metric_value=1.2,
            confidence_score=35,
            missing_context=["trace_id", "related_logs"],
            context_completeness=0.25,
            skip_side_effects=True,
        )
    )
    eng.close()
    assert d.action == DecisionAction.ESCALATE_ONCALL
    assert d.escalated
    assert d.escalate_reason


def test_engine_medium_llm_path():
    eng = DecisionEngine()
    d = eng.decide(
        DecideRequest(
            service_name="checkout-service",
            metric_name="http_request_rate",
            metric_value=0.0,
            confidence_score=70,
            missing_context=[],  # no critical missing
            context_completeness=0.75,
            explanation="request rate drop",
            skip_side_effects=True,
        )
    )
    eng.close()
    assert d.action == DecisionAction.RCA_SUGGEST
    assert d.llm_called  # path selected; side effects skipped still marks intent
    assert d.suggestions  # static suggestions when skip_side_effects
