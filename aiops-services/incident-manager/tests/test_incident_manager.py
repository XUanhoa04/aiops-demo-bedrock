"""
Local tests for Incident Manager (no Docker required).

Run from repo root:
  pip install -r shared/requirements-base.txt -r aiops-services/incident-manager/requirements.txt
  set PYTHONPATH=shared;aiops-services/incident-manager   # Windows
  pytest aiops-services/incident-manager/tests -q

Or:
  python aiops-services/incident-manager/tests/test_incident_manager.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make app + shared importable when run as a script
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "incident-manager"))

# Point SQLite at a temp file before importing app modules that read settings
_TMPDIR = tempfile.mkdtemp(prefix="aiops-im-")
os.environ["INCIDENT_DB_PATH"] = str(Path(_TMPDIR) / "test_incidents.db")
os.environ["REDIS_URL"] = "redis://127.0.0.1:6399/15"  # unlikely; fan-out mocked
os.environ["RCA_ENGINE_URL"] = ""

from aiops_shared.models import AnomalyEvent, AnomalySeverity, IncidentStatus  # noqa: E402

from app.db import IncidentRepository, incident_from_anomaly  # noqa: E402
from app.prom_metrics import INCIDENTS_CREATED, OPEN_INCIDENTS  # noqa: E402


class TestIncidentRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = str(Path(_TMPDIR) / f"repo_{self.id().split('.')[-1]}.db")
        self.repo = IncidentRepository(db_path=self.db_path)

    def test_insert_get_list(self) -> None:
        anomaly = AnomalyEvent(
            service_name="checkout-service",
            metric_name="http_error_rate",
            metric_value=0.42,
            threshold=0.15,
            severity=AnomalySeverity.HIGH,
            detector="hybrid:manual",
            message="test anomaly",
        )
        incident = incident_from_anomaly(anomaly)
        self.repo.insert(incident)

        got = self.repo.get(incident.id)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got.service_name, "checkout-service")
        self.assertEqual(got.status, IncidentStatus.OPEN)
        self.assertIn("anomaly_details", got.context)
        self.assertEqual(got.context["anomaly_details"]["metric_value"], 0.42)

        listed = self.repo.list(service_name="checkout-service")
        self.assertEqual(len(listed), 1)
        self.assertEqual(self.repo.count_open(), 1)

    def test_correlation_window(self) -> None:
        a1 = AnomalyEvent(
            service_name="payment-service",
            metric_name="http_error_rate",
            metric_value=0.3,
            threshold=0.15,
            severity=AnomalySeverity.MEDIUM,
            message="first",
        )
        inc = incident_from_anomaly(a1)
        self.repo.insert(inc)

        found = self.repo.find_open_correlated(
            service_name="payment-service",
            metric_name="http_error_rate",
            window_minutes=10,
        )
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.id, inc.id)

        # Different metric → no correlate
        other = self.repo.find_open_correlated(
            service_name="payment-service",
            metric_name="latency_p95",
            window_minutes=10,
        )
        self.assertIsNone(other)


class TestHandleAnomaly(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = str(Path(_TMPDIR) / f"consumer_{self.id().split('.')[-1]}.db")
        self.repo = IncidentRepository(db_path=self.db_path)

    def test_create_and_correlate(self) -> None:
        from app.consumer import AnomalyConsumer
        from app.rca_client import RCAClient

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.lpush.return_value = 1

        with patch("app.consumer.get_redis", return_value=mock_redis):
            consumer = AnomalyConsumer(self.repo, rca=RCAClient(""))
            consumer.redis = mock_redis

            a1 = AnomalyEvent(
                service_name="checkout-service",
                metric_name="http_error_rate",
                metric_value=0.5,
                threshold=0.15,
                severity=AnomalySeverity.HIGH,
                message="spike #1",
            )
            i1 = consumer.handle_anomaly(a1, source="webhook")
            self.assertEqual(i1.status, IncidentStatus.OPEN)
            self.assertEqual(self.repo.count_open(), 1)

            a2 = AnomalyEvent(
                service_name="checkout-service",
                metric_name="http_error_rate",
                metric_value=0.6,
                threshold=0.15,
                severity=AnomalySeverity.CRITICAL,
                message="spike #2",
            )
            i2 = consumer.handle_anomaly(a2, source="redis")
            self.assertEqual(i2.id, i1.id, "should correlate into same ticket")
            self.assertEqual(i2.severity, AnomalySeverity.CRITICAL)
            self.assertEqual(int(i2.context.get("occurrence_count", 0)), 2)
            self.assertEqual(self.repo.count_open(), 1)

            # metrics recorded
            self.assertGreaterEqual(OPEN_INCIDENTS._value.get(), 1)

    def test_single_control_plane_no_direct_rca_when_decision_ok(self) -> None:
        """Decision Engine is primary; direct RCA stays off by default."""
        from app.consumer import AnomalyConsumer
        from app import config as cfg

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.lpush.return_value = 1

        mock_decision = MagicMock()
        mock_decision.enabled = True
        mock_decision.push.return_value = {"ok": True}

        mock_rca = MagicMock()
        mock_rca.enabled = True

        prev = cfg.settings.enable_direct_rca_fanout
        cfg.settings.enable_direct_rca_fanout = False
        try:
            with patch("app.consumer.get_redis", return_value=mock_redis):
                consumer = AnomalyConsumer(
                    self.repo, rca=mock_rca, decision=mock_decision
                )
                consumer.redis = mock_redis
                a1 = AnomalyEvent(
                    service_name="checkout-service",
                    metric_name="http_error_rate",
                    metric_value=0.4,
                    threshold=0.15,
                    severity=AnomalySeverity.HIGH,
                    message="control plane test",
                )
                consumer.handle_anomaly(a1, source="webhook")
                mock_decision.push.assert_called_once()
                mock_rca.push_incident.assert_not_called()
        finally:
            cfg.settings.enable_direct_rca_fanout = prev

    def test_direct_rca_when_flag_enabled(self) -> None:
        from app.consumer import AnomalyConsumer
        from app import config as cfg

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.lpush.return_value = 1
        mock_decision = MagicMock()
        mock_decision.enabled = True
        mock_decision.push.return_value = {"ok": True}
        mock_rca = MagicMock()
        mock_rca.enabled = True

        prev = cfg.settings.enable_direct_rca_fanout
        cfg.settings.enable_direct_rca_fanout = True
        try:
            with patch("app.consumer.get_redis", return_value=mock_redis):
                consumer = AnomalyConsumer(
                    self.repo, rca=mock_rca, decision=mock_decision
                )
                consumer.redis = mock_redis
                a1 = AnomalyEvent(
                    service_name="payment-service",
                    metric_name="http_error_rate",
                    metric_value=0.5,
                    threshold=0.15,
                    severity=AnomalySeverity.HIGH,
                    message="legacy fanout",
                )
                consumer.handle_anomaly(a1, source="webhook")
                mock_rca.push_incident.assert_called_once()
        finally:
            cfg.settings.enable_direct_rca_fanout = prev


class TestAPIWithTestClient(unittest.TestCase):
    """FastAPI TestClient — exercises REST without real Redis (fan-out mocked)."""

    @classmethod
    def setUpClass(cls) -> None:
        # Re-bind modules after env is set
        from fastapi.testclient import TestClient

        from app.consumer import AnomalyConsumer
        from app import main as main_mod

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.lpush.return_value = 1
        mock_redis.brpop.return_value = None

        # Prevent real Redis connection in consumer loop
        main_mod.consumer.redis = mock_redis
        main_mod.repo = IncidentRepository(
            db_path=str(Path(_TMPDIR) / "api_incidents.db")
        )
        main_mod.consumer = AnomalyConsumer(main_mod.repo, rca=main_mod.rca)
        main_mod.consumer.redis = mock_redis

        # Don't start background consumer against real Redis
        async def _noop_start():
            return None

        async def _noop_stop():
            return None

        main_mod.consumer.start = _noop_start  # type: ignore[method-assign]
        main_mod.consumer.stop = _noop_stop  # type: ignore[method-assign]

        cls.client = TestClient(main_mod.app)
        cls.main_mod = main_mod

    def test_health_and_metrics(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["service"], "aiops-incident-manager")

        m = self.client.get("/metrics")
        self.assertEqual(m.status_code, 200)
        text = m.text
        self.assertIn("incidents_created_total", text)
        self.assertIn("open_incidents", text)

    def test_manual_crud(self) -> None:
        r = self.client.post(
            "/incidents",
            json={
                "title": "[MANUAL] test",
                "description": "unit test",
                "service_name": "checkout-service",
                "severity": "medium",
            },
        )
        self.assertEqual(r.status_code, 201)
        inc = r.json()
        iid = inc["id"]

        r2 = self.client.get(f"/incidents/{iid}")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.json()["title"], "[MANUAL] test")

        r3 = self.client.get("/incidents", params={"service_name": "checkout-service"})
        self.assertEqual(r3.status_code, 200)
        self.assertTrue(any(x["id"] == iid for x in r3.json()))

    def test_from_anomaly_webhook(self) -> None:
        payload = {
            "service_name": "payment-service",
            "metric_name": "http_error_rate",
            "metric_value": 0.55,
            "threshold": 0.15,
            "severity": "high",
            "detector": "hybrid:manual",
            "message": "webhook test anomaly",
        }
        r = self.client.post("/incidents/from-anomaly", json=payload)
        self.assertEqual(r.status_code, 201)
        inc = r.json()
        self.assertEqual(inc["service_name"], "payment-service")
        self.assertIn("anomaly_details", inc.get("context", {}))

    def test_ui_stub(self) -> None:
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("AIOps", r.text)
        # Console title (not the service process name)
        self.assertTrue(
            "Incident Console" in r.text or "Incident Manager" in r.text,
            msg="UI should mention Incident Console/Manager",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
