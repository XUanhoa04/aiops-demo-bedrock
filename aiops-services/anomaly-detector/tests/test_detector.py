"""Unit tests for hybrid detector (EWMA / z-score / threshold / optional STL)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detector import HybridDetector


def test_ewma_flags_level_shift():
    det = HybridDetector()
    # Warm-up healthy baseline
    for _ in range(12):
        det.evaluate_service(
            "checkout-service",
            {
                "http_error_rate": 0.01,
                "http_request_rate": 10.0,
                "http_latency_p95_seconds": 0.05,
            },
        )
    # Spike error rate
    results = det.evaluate_service(
        "checkout-service",
        {
            "http_error_rate": 0.55,
            "http_request_rate": 10.0,
            "http_latency_p95_seconds": 0.05,
        },
    )
    err = next(r for r in results if r.metric == "http_error_rate")
    assert err.is_anomaly
    assert err.anomaly_score > 0
    assert err.explanation
    assert any(m.method in {"ewma_zscore", "zscore", "threshold"} for m in err.methods)
    # Every anomalous method should carry an explanation string
    for m in err.methods:
        if m.is_anomaly:
            assert m.explanation


def test_manual_force_score():
    det = HybridDetector()
    r = det.force_score("payment-service", "http_error_rate", 0.5, 0.15)
    assert r.is_anomaly
    assert "manual" in r.winning_methods
    assert "threshold" in r.explanation.lower() or "Manual" in r.explanation or "0.5" in r.explanation


def test_isolation_forest_warms_up():
    det = HybridDetector()
    last = None
    for i in range(15):
        # Mostly normal joint vector
        features = {
            "http_error_rate": 0.02 + (0.001 * (i % 3)),
            "http_request_rate": 8.0 + i * 0.01,
            "http_latency_p95_seconds": 0.04,
        }
        last = det.evaluate_service("svc-if", features)
    # After warm-up, multivariate result should exist
    assert last is not None
    mv = [r for r in last if r.metric.startswith("multivariate:")]
    # May or may not be anomaly depending on contamination; method must be present after min_samples
    assert mv, "expected multivariate IsolationForest result after warm-up"
