"""
Unit tests for the Feedback Collector service.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Point feedback SQLite at a temporary file before loading application modules
_TMP_DIR = tempfile.mkdtemp(prefix="feedback-test-")
_TEST_DB = str(Path(_TMP_DIR) / "test_feedback.db")
os.environ["FEEDBACK_DB_PATH"] = _TEST_DB
os.environ["OTEL_SDK_DISABLED"] = "true"

from app.config import settings
from app.db import FeedbackRepository
from app.main import app, svc
from app.models import FeedbackCreate, FeedbackRecord, FeedbackStats, TuningSuggestion
from app.service import FeedbackService
from app.tuning import format_tuning_report, suggest_threshold_adjustments


@pytest.fixture
def temp_repo(tmp_path: Path) -> FeedbackRepository:
    db_file = str(tmp_path / "repo.db")
    return FeedbackRepository(db_path=db_file)


class TestFeedbackRepository:
    def test_empty_stats(self, temp_repo: FeedbackRepository) -> None:
        stats = temp_repo.compute_stats()
        assert stats.total == 0
        assert stats.with_anomaly_vote == 0
        assert stats.with_rca_vote == 0
        assert stats.with_action_vote == 0
        assert stats.false_positive_count == 0
        assert stats.feedback_positive_rate == 0.0

    def test_insert_and_get(self, temp_repo: FeedbackRepository) -> None:
        rec = FeedbackRecord(
            incident_id="inc-101",
            anomaly_correct=True,
            rca_useful=False,
            action_effective=True,
            comment="Root cause was wrong service but action helped",
            reviewer="alice",
            service_name="payment-service",
            severity="high",
            incident_status="resolved",
        )
        saved = temp_repo.insert(rec)
        assert saved.id == rec.id

        fetched = temp_repo.get(rec.id)
        assert fetched is not None
        assert fetched.incident_id == "inc-101"
        assert fetched.anomaly_correct is True
        assert fetched.rca_useful is False
        assert fetched.action_effective is True
        assert fetched.service_name == "payment-service"
        assert fetched.reviewer == "alice"
        assert fetched.is_false_positive is False

    def test_list_and_filter(self, temp_repo: FeedbackRepository) -> None:
        temp_repo.insert(FeedbackRecord(incident_id="inc-1", comment="First"))
        temp_repo.insert(FeedbackRecord(incident_id="inc-2", comment="Second"))
        temp_repo.insert(FeedbackRecord(incident_id="inc-1", comment="Third"))

        all_records = temp_repo.list(limit=10)
        assert len(all_records) == 3

        filtered = temp_repo.list(incident_id="inc-1", limit=10)
        assert len(filtered) == 2
        assert {r.comment for r in filtered} == {"First", "Third"}

    def test_false_positive_tracking_and_stats(self, temp_repo: FeedbackRepository) -> None:
        # 1: True anomaly, helpful RCA, effective action
        temp_repo.insert(
            FeedbackRecord(
                incident_id="inc-1",
                anomaly_correct=True,
                rca_useful=True,
                action_effective=True,
                service_name="checkout-service",
            )
        )
        # 2: False positive anomaly (anomaly_correct=False)
        temp_repo.insert(
            FeedbackRecord(
                incident_id="inc-2",
                anomaly_correct=False,
                rca_useful=None,
                action_effective=None,
                service_name="fraud-service",
                comment="Noise from periodic cron",
            )
        )
        # 3: True anomaly, wrong RCA, effective action
        temp_repo.insert(
            FeedbackRecord(
                incident_id="inc-3",
                anomaly_correct=True,
                rca_useful=False,
                action_effective=True,
                service_name="checkout-service",
            )
        )

        fps = temp_repo.list_false_positives()
        assert len(fps) == 1
        assert fps[0].incident_id == "inc-2"
        assert fps[0].is_false_positive is True
        assert fps[0].service_name == "fraud-service"

        stats = temp_repo.compute_stats()
        assert stats.total == 3
        assert stats.with_anomaly_vote == 3
        assert stats.anomaly_positive == 2
        assert stats.false_positive_count == 1
        assert stats.with_rca_vote == 2
        assert stats.rca_positive == 1
        assert stats.with_action_vote == 2
        assert stats.action_positive == 2

        # 2 positive anomaly + 1 positive rca + 2 positive action = 5 thumbs up out of 7 cast
        assert stats.feedback_positive_rate == round(5 / 7, 4)
        assert stats.rca_accuracy_estimate == 0.5
        assert stats.anomaly_precision_estimate == round(2 / 3, 4)
        assert stats.action_success_rate == 1.0


class TestTuningLogic:
    def test_insufficient_samples(self, temp_repo: FeedbackRepository) -> None:
        # Less than min_samples_for_tuning (default 5)
        temp_repo.insert(
            FeedbackRecord(incident_id="inc-1", anomaly_correct=False, service_name="cart")
        )
        suggestion = suggest_threshold_adjustments(temp_repo)
        assert suggestion.suggested_zscore_threshold is None
        assert suggestion.suggested_error_rate_threshold is None
        assert "Insufficient samples" in suggestion.recommendation
        assert suggestion.anomaly_votes == 1
        assert suggestion.false_positive_count == 1

    def test_high_fp_rate_triggers_increase(self, temp_repo: FeedbackRepository) -> None:
        # 6 samples: 3 false positives (50% FP rate > 20% warn threshold)
        for i in range(3):
            temp_repo.insert(
                FeedbackRecord(
                    incident_id=f"inc-tp-{i}",
                    anomaly_correct=True,
                    service_name="checkout-service",
                )
            )
        for i in range(3):
            temp_repo.insert(
                FeedbackRecord(
                    incident_id=f"inc-fp-{i}",
                    anomaly_correct=False,
                    service_name="inventory-service",
                    comment=f"Spurious alert {i}",
                )
            )

        suggestion = suggest_threshold_adjustments(temp_repo)
        assert suggestion.anomaly_votes == 6
        assert suggestion.false_positive_count == 3
        assert suggestion.false_positive_rate == 0.5
        assert suggestion.suggested_zscore_threshold is not None
        assert suggestion.suggested_zscore_threshold > suggestion.current_zscore_threshold
        assert suggestion.suggested_error_rate_threshold is not None
        assert suggestion.suggested_error_rate_threshold > suggestion.current_error_rate_threshold
        assert "inventory-service" in suggestion.sample_fp_services
        assert "False positives are high" in suggestion.recommendation

        report = format_tuning_report(suggestion)
        assert "=== AIOps threshold tuning suggestion ===" in report
        assert "Suggested ZSCORE:" in report
        assert "inventory-service" in report

    def test_low_fp_high_precision_suggests_mild_decrease(
        self, temp_repo: FeedbackRepository
    ) -> None:
        # 10 samples: 10 true positives, 0 FP (precision 1.0, FP rate 0.0)
        for i in range(10):
            temp_repo.insert(
                FeedbackRecord(
                    incident_id=f"inc-{i}",
                    anomaly_correct=True,
                    service_name="payment-service",
                )
            )

        suggestion = suggest_threshold_adjustments(temp_repo)
        assert suggestion.anomaly_votes == 10
        assert suggestion.false_positive_count == 0
        assert suggestion.false_positive_rate == 0.0
        assert suggestion.suggested_zscore_threshold is not None
        assert suggestion.suggested_zscore_threshold < suggestion.current_zscore_threshold
        assert "Precision looks good" in suggestion.recommendation

    def test_balanced_feedback_suggests_no_change(
        self, temp_repo: FeedbackRepository
    ) -> None:
        # 10 samples: 9 TP, 1 FP (FP rate = 10%, between 5% and 20% warn threshold)
        for i in range(9):
            temp_repo.insert(
                FeedbackRecord(
                    incident_id=f"inc-tp-{i}",
                    anomaly_correct=True,
                    service_name="payment-service",
                )
            )
        temp_repo.insert(
            FeedbackRecord(
                incident_id="inc-fp-1",
                anomaly_correct=False,
                service_name="payment-service",
            )
        )

        suggestion = suggest_threshold_adjustments(temp_repo)
        assert suggestion.suggested_zscore_threshold is None
        assert suggestion.suggested_error_rate_threshold is None
        assert "balanced" in suggestion.recommendation.lower()


class TestFeedbackService:
    def test_submit_captures_incident_metadata(self, temp_repo: FeedbackRepository) -> None:
        mock_client = MagicMock()
        mock_client.get_incident.return_value = {
            "id": "inc-456",
            "service_name": "checkout-service",
            "severity": "CRITICAL",
            "status": "investigating",
        }

        service = FeedbackService(repo=temp_repo, incidents=mock_client)
        req = FeedbackCreate(
            incident_id="inc-456",
            anomaly_correct=True,
            rca_useful=True,
            action_effective=True,
            comment="Spot-on diagnosis",
            reviewer="bob",
        )
        rec = service.submit(req)

        assert rec.incident_id == "inc-456"
        assert rec.service_name == "checkout-service"
        assert rec.severity == "CRITICAL"
        assert rec.incident_status == "investigating"
        mock_client.apply_feedback.assert_called_once()
        service.close()
        mock_client.close.assert_called_once()

    def test_submit_handles_incident_lookup_error(self, temp_repo: FeedbackRepository) -> None:
        mock_client = MagicMock()
        mock_client.get_incident.side_effect = LookupError("Not found")

        service = FeedbackService(repo=temp_repo, incidents=mock_client)
        req = FeedbackCreate(
            incident_id="missing-inc",
            anomaly_correct=False,
            comment="Unknown incident review",
        )
        rec = service.submit(req)
        assert rec.incident_id == "missing-inc"
        assert rec.service_name is None
        assert rec.is_false_positive is True


class TestFeedbackAPI:
    @pytest.fixture(autouse=True)
    def setup_client(self, temp_repo: FeedbackRepository) -> None:
        self.mock_incidents = MagicMock()
        self.mock_incidents.healthy.return_value = True
        self.mock_incidents.get_incident.return_value = {
            "id": "inc-api-1",
            "service_name": "auth-service",
            "severity": "MEDIUM",
            "status": "closed",
        }
        svc.repo = temp_repo
        svc.incidents = self.mock_incidents
        self.client = TestClient(app)

    def test_health_and_ready(self) -> None:
        res = self.client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["service"] == "aiops-feedback-collector"

        res = self.client.get("/ready")
        assert res.status_code == 200
        assert res.json() == {"ready": True}

        self.mock_incidents.healthy.return_value = False
        res = self.client.get("/ready")
        assert res.status_code == 503

    def test_submit_validation(self) -> None:
        # Sending empty request without thumbs or comments should return 400
        res = self.client.post("/feedback", json={"incident_id": "inc-none"})
        assert res.status_code == 400

    def test_submit_and_query_endpoints(self) -> None:
        payload = {
            "incident_id": "inc-api-1",
            "anomaly_correct": True,
            "rca_useful": True,
            "action_effective": False,
            "comment": "RCA was good, action need manual step",
            "reviewer": "charlie",
            "corrected_root_cause": "Database connection pool exhausted",
        }
        res = self.client.post("/feedback", json=payload)
        assert res.status_code == 201
        created = res.json()
        fb_id = created["id"]
        assert created["incident_id"] == "inc-api-1"
        assert created["reviewer"] == "charlie"

        # Query single
        res = self.client.get(f"/feedback/{fb_id}")
        assert res.status_code == 200
        assert res.json()["id"] == fb_id

        # Query missing
        res = self.client.get("/feedback/does-not-exist")
        assert res.status_code == 404

        # List feedback
        res = self.client.get("/feedback?incident_id=inc-api-1")
        assert res.status_code == 200
        records = res.json()
        assert len(records) >= 1
        assert records[0]["id"] == fb_id

        # Stats
        res = self.client.get("/stats")
        assert res.status_code == 200
        stats = res.json()
        assert stats["total"] >= 1
        assert stats["with_anomaly_vote"] >= 1

        # Tuning report & suggestions
        res = self.client.get("/tuning/suggestions")
        assert res.status_code == 200
        assert "recommendation" in res.json()

        res = self.client.get("/tuning/report")
        assert res.status_code == 200
        assert "=== AIOps threshold tuning suggestion ===" in res.text

        # Metrics scrape
        res = self.client.get("/metrics")
        assert res.status_code == 200
        assert "feedback_positive_rate" in res.text
