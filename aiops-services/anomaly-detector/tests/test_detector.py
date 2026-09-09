"""Unit tests for hybrid detector (EWMA / z-score / threshold / optional STL)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.detector import HybridDetector
from app.config import settings


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


def test_explicit_api_threshold_score():
    det = HybridDetector()
    r = det.force_score("payment-service", "http_error_rate", 0.5, 0.15)
    assert r.is_anomaly
    assert "api_threshold" in r.winning_methods
    assert "threshold" in r.explanation.lower() or "0.5" in r.explanation


def test_current_sample_is_not_leaked_into_rolling_baseline():
    det = HybridDetector()
    for _ in range(settings.min_samples):
        det._score_univariate("checkout-service", "http_request_rate", 10.0)

    result = det._score_univariate(
        "checkout-service", "http_request_rate", 100.0
    )
    zscore = next(m for m in result.methods if m.method == "zscore")
    assert zscore.is_anomaly
    assert zscore.detail["baseline_mean"] == 10.0


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


def test_checkpoint_restore_preserves_baselines_and_feature_history():
    original = HybridDetector()
    for i in range(settings.min_samples + 2):
        original.evaluate_service(
            "checkout-service",
            {
                "http_error_rate": 0.01 + i / 10000,
                "http_request_rate": 10.0 + i / 100,
                "http_latency_p95_seconds": 0.05,
            },
        )

    checkpoint = original.snapshot()
    restored = HybridDetector()
    counts = restored.restore(checkpoint)

    assert counts["series"] == 3
    assert counts["feature_rows"] == settings.min_samples + 2
    state = restored._series["checkout-service:http_error_rate"]
    assert len(state.values) == settings.min_samples + 2
    assert state.ewma is not None

    results = restored.evaluate_service(
        "checkout-service",
        {
            "http_error_rate": 0.8,
            "http_request_rate": 10.0,
            "http_latency_p95_seconds": 0.05,
        },
    )
    err = next(r for r in results if r.metric == "http_error_rate")
    assert any(m.method == "zscore" for m in err.methods)
    assert any(r.metric.startswith("multivariate:") for r in results)


def test_checkpoint_rejects_unknown_schema():
    det = HybridDetector()
    try:
        det.restore({"schema_version": 999})
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("unknown checkpoint schema must be rejected")


def test_iforest_contamination_defaults_to_auto():
    previous = settings.iforest_contamination
    try:
        settings.iforest_contamination = "auto"
        assert settings.iforest_contamination_value == "auto"
        settings.iforest_contamination = "0.08"
        assert settings.iforest_contamination_value == 0.08
    finally:
        settings.iforest_contamination = previous


def test_state_store_round_trip_restores_checkpoint():
    from app.state_store import DetectorStateStore

    class FakeRedis:
        value = None

        def set(self, key, value):
            self.value = value
            return True

        def get(self, key):
            return self.value

    original = HybridDetector()
    for _ in range(settings.min_samples):
        original.evaluate_service(
            "svc-persist",
            {
                "http_error_rate": 0.01,
                "http_request_rate": 5.0,
                "http_latency_p95_seconds": 0.1,
            },
        )
    redis = FakeRedis()
    store = DetectorStateStore(redis_client=redis)
    assert store.save(original)

    restored = HybridDetector()
    assert store.restore_into(restored)
    assert len(restored._series["svc-persist:http_error_rate"].values) == settings.min_samples


def test_snapshot_handles_nan_and_inf_safely():
    import json
    import math

    det = HybridDetector()
    det.evaluate_service(
        "svc-nan",
        {
            "http_error_rate": 0.05,
            "http_request_rate": 10.0,
            "http_latency_p95_seconds": 0.02,
        },
    )
    # Force non-finite values into series state
    state = det._series["svc-nan:http_error_rate"]
    state.ewma = float("nan")
    state.ewma_var = float("inf")

    snap = det.snapshot()
    # json.dumps with allow_nan=False must not raise ValueError
    serialized = json.dumps(snap, allow_nan=False)
    assert serialized is not None

    restored = HybridDetector()
    counts = restored.restore(json.loads(serialized))
    assert counts["series"] == 3
    restored_state = restored._series["svc-nan:http_error_rate"]
    # Non-finite EWMA must have fallen back to last finite sample value safely
    assert math.isfinite(restored_state.ewma)
    assert restored_state.ewma == 0.05


def test_alert_cooldown_eviction():
    import time
    from unittest.mock import MagicMock
    from app.worker import DetectorWorker
    from app.detector import HybridResult
    from app.models import DetectionDecision

    worker = DetectorWorker()
    worker.notifier = MagicMock()
    worker.decisions = MagicMock()

    res = HybridResult(
        service="checkout-service",
        metric="http_error_rate",
        value=0.5,
        is_anomaly=True,
        anomaly_score=3.5,
        methods=[],
        features={},
        winning_methods=["zscore"],
    )
    decision = DetectionDecision(
        service_name="checkout-service",
        metric_name="http_error_rate",
        metric_value=0.5,
        is_anomaly=True,
        anomaly_score=3.5,
        detection_method="zscore",
        confidence_score=90.0,
        explanation="Test anomaly",
        signals={},
        detection_methods=["zscore"],
        missing_context=[],
        context_completeness=1.0,
    )

    # Seed 105 old entries in _last_fired
    now = time.time()
    old_ts = now - (settings.alert_cooldown_sec * 3)
    for i in range(105):
        worker._last_fired[f"svc-{i}:metric"] = old_ts

    # Trigger notify
    worker._maybe_notify(res, decision)
    # Old entries should be evicted, leaving only recent ones
    assert len(worker._last_fired) < 10
    assert "checkout-service:http_error_rate" in worker._last_fired
