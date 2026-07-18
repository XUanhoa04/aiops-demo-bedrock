"""Unit tests for pure policy (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))

from app.decision_table import (  # noqa: E402
    ConfidenceBand,
    DecisionAction,
    confidence_band,
    select_action,
    table_as_markdown,
)
from app.patterns import find_known_pattern  # noqa: E402


def test_bands():
    assert confidence_band(90) == ConfidenceBand.HIGH
    assert confidence_band(85) == ConfidenceBand.HIGH
    assert confidence_band(70) == ConfidenceBand.MEDIUM
    assert confidence_band(60) == ConfidenceBand.MEDIUM
    assert confidence_band(59.9) == ConfidenceBand.LOW


def test_high_with_pattern_auto():
    action, band, reason = select_action(
        confidence=90,
        missing_context=[],
        has_known_pattern=True,
        iteration=1,
        max_iterations=3,
    )
    assert action == DecisionAction.AUTO_REMEDIATE_GATED
    assert band == ConfidenceBand.HIGH
    assert "gated" in reason.lower() or "pattern" in reason.lower()


def test_high_without_pattern_rca():
    action, band, _ = select_action(
        confidence=90,
        missing_context=[],
        has_known_pattern=False,
        iteration=1,
        max_iterations=3,
    )
    assert action == DecisionAction.RCA_SUGGEST
    assert band == ConfidenceBand.HIGH


def test_medium_calls_rca():
    action, band, reason = select_action(
        confidence=72,
        missing_context=[],
        has_known_pattern=False,
        iteration=1,
        max_iterations=3,
    )
    assert action == DecisionAction.RCA_SUGGEST
    assert band == ConfidenceBand.MEDIUM
    assert "Bedrock" in reason or "RCA" in reason


def test_low_escalates():
    action, band, reason = select_action(
        confidence=40,
        missing_context=[],
        has_known_pattern=True,
        iteration=1,
        max_iterations=3,
    )
    assert action == DecisionAction.ESCALATE_ONCALL
    assert band == ConfidenceBand.LOW
    assert "escalate" in reason.lower()


def test_missing_critical_escalates_even_if_high():
    action, band, reason = select_action(
        confidence=95,
        missing_context=["sufficient_metrics"],
        has_known_pattern=True,
        iteration=1,
        max_iterations=3,
    )
    assert action == DecisionAction.ESCALATE_ONCALL
    assert "critical" in reason.lower() or "Missing" in reason


def test_iteration_exhausted():
    action, _, reason = select_action(
        confidence=70,
        missing_context=[],
        has_known_pattern=False,
        iteration=4,
        max_iterations=3,
    )
    assert action == DecisionAction.HANDOFF_EXHAUSTED
    assert "exhausted" in reason.lower()


def test_known_pattern_error_rate():
    m = find_known_pattern(
        service_name="checkout-service",
        metric_name="http_error_rate",
        metric_value=0.45,
        explanation="error rate cao hơn 3 sigma",
    )
    assert m is not None
    assert m.pattern.id == "error_rate_spike_reset"
    assert "checkout-service" in m.action_text


def test_table_markdown():
    md = table_as_markdown()
    assert "auto_remediate_gated" in md
    assert "rca_suggest" in md
    assert "escalate_oncall" in md
