"""Unit tests for confidence scoring + context completeness (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python -m pytest` from repo root or service dir without install.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.confidence_scorer import ConfidenceScorer, compute_context_completeness
from app.models import SignalBundle


def _full_signals() -> SignalBundle:
    return SignalBundle(
        metrics={
            "instant": {
                "http_error_rate": 0.4,
                "http_request_rate": 12.0,
                "http_latency_p95_seconds": 1.2,
            },
            "range": {
                "http_error_rate": {"points": 20, "max": 0.4, "avg": 0.1},
            },
        },
        logs=[
            {"line": "ERROR payment failed", "trace_id": "a" * 32},
            {"line": "exception in checkout", "trace_id": "b" * 32},
        ],
        traces=[
            {
                "trace_id": "a" * 32,
                "duration_ms": 900,
                "search_mode": "traceql_error",
            }
        ],
        events=[{"type": "chaos", "severity": "high", "message": "chaos inject"}],
        primary_trace_id="a" * 32,
        sources_ok={"prometheus": True, "loki": True, "tempo": True},
    )


def test_completeness_full():
    s = _full_signals()
    c = compute_context_completeness(s)
    assert c.has_trace_id
    assert c.has_related_logs
    assert c.has_sufficient_metrics
    assert c.has_events
    assert c.ratio == 1.0
    assert c.missing == []


def test_completeness_metrics_only():
    s = SignalBundle(
        metrics={"instant": {"http_error_rate": 0.5, "http_request_rate": 1.0}},
        sources_ok={"prometheus": True},
    )
    c = compute_context_completeness(s)
    assert c.has_sufficient_metrics
    assert not c.has_trace_id
    assert not c.has_related_logs
    assert "trace_id" in c.missing
    assert "related_logs" in c.missing
    assert c.ratio == 0.25


def test_confidence_high_with_full_context():
    s = _full_signals()
    s.completeness = compute_context_completeness(s)
    scorer = ConfidenceScorer(0.4, 0.3, 0.2, 0.1)
    result = scorer.score(
        anomaly_score=3.5,
        is_anomaly=True,
        winning_methods=["ewma_zscore", "threshold"],
        signals=s,
        completeness=s.completeness,
        primary_metric="http_error_rate",
    )
    assert result.confidence_score >= 55
    assert result.missing_context == []
    bd = result.confidence_breakdown
    assert bd.metrics > 0
    assert bd.traces > 0
    assert bd.logs > 0
    assert bd.events > 0
    assert abs(sum(bd.weights.values()) - 1.0) < 1e-6


def test_confidence_penalized_when_missing_context():
    s = SignalBundle(
        metrics={"instant": {"http_error_rate": 0.45}},
        sources_ok={"prometheus": True, "loki": False, "tempo": False},
    )
    s.completeness = compute_context_completeness(s)
    scorer = ConfidenceScorer(0.4, 0.3, 0.2, 0.1)
    result = scorer.score(
        anomaly_score=4.0,
        is_anomaly=True,
        winning_methods=["ewma_zscore"],
        signals=s,
        completeness=s.completeness,
        primary_metric="http_error_rate",
    )
    assert result.confidence_score < 55
    assert "trace_id" in result.missing_context
    assert result.confidence_breakdown.penalties > 0
    assert any("missing_trace" in r or "source_down" in r for r in result.confidence_breakdown.penalty_reasons)


def test_weights_normalize():
    scorer = ConfidenceScorer(40, 30, 20, 10)
    assert abs(scorer.w_metrics - 0.4) < 1e-9
    assert abs(scorer.w_traces - 0.3) < 1e-9
