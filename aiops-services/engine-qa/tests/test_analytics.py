"""Unit tests for quality aggregates + tuning (no network)."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "shared"))

from app.analytics import compute_quality  # noqa: E402
from app.db import QARepository  # noqa: E402
from app.models import QAReview  # noqa: E402
from app.tuning import suggest_tuning  # noqa: E402


def _rev(**kwargs) -> QAReview:
    base = dict(
        incident_id="inc-1",
        service_name="checkout-service",
        severity="high",
        reviewer="tester",
    )
    base.update(kwargs)
    return QAReview(**base)


def test_precision_and_fp():
    rows = [
        _rev(anomaly_correct=True, engine_confidence=80),
        _rev(anomaly_correct=True, engine_confidence=75),
        _rev(anomaly_correct=False, engine_confidence=90),  # FP + overconfident
        _rev(anomaly_correct=True, engine_confidence=70),
    ]
    q = compute_quality(rows)
    assert q.anomaly_votes == 4
    assert q.anomaly_true_positive == 3
    assert q.anomaly_false_positive == 1
    assert abs(q.precision_estimate - 0.75) < 1e-6
    assert abs(q.false_positive_rate - 0.25) < 1e-6
    assert q.overconfidence_count == 1


def test_hallucination_proxy():
    rows = [
        _rev(rca_useful=True),
        _rev(rca_useful=False, corrected_root_cause="Actually payment timeout"),
        _rev(rca_useful=False, llm_hallucinated=True),
        _rev(rca_useful=True),
    ]
    q = compute_quality(rows)
    assert q.rca_votes == 4
    assert q.hallucination_count == 2
    assert q.hallucination_rate == 0.5


def test_mean_iterations():
    rows = [
        _rev(decision_iterations=1, decision_correct=True),
        _rev(decision_iterations=3, decision_correct=False),
        _rev(decision_iterations=2, decision_correct=True),
    ]
    q = compute_quality(rows)
    assert abs(q.mean_decision_iterations - 2.0) < 1e-6
    assert abs(q.decision_correct_rate - 2 / 3) < 1e-3


def test_tuning_raises_on_high_fp():
    rows = [
        _rev(anomaly_correct=False, engine_confidence=85, severity="medium")
        for _ in range(6)
    ]
    advice = suggest_tuning(rows)
    assert advice.sample_size == 6
    assert advice.suggested_zscore_threshold is not None
    assert advice.suggested_zscore_threshold > advice.current_zscore_threshold
    assert "ZSCORE" in advice.env_snippet or "ZSCORE" in advice.recommendation + "".join(
        advice.details
    )


def test_repo_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "qa.db")
        repo = QARepository(db_path=db)
        r = _rev(
            anomaly_correct=True,
            confidence_reasonable=True,
            rca_useful=True,
            decision_correct=True,
            decision_iterations=2,
            engine_confidence=77,
            missing_context=["events_or_change"],
            confidence_breakdown={"metrics": 30},
        )
        repo.insert(r)
        assert repo.count() == 1
        got = repo.list_reviews(limit=10)[0]
        assert got.anomaly_correct is True
        assert got.engine_confidence == 77
        assert got.missing_context == ["events_or_change"]
