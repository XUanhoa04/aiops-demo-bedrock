"""Remediation classifier + operator API-key gate (no Docker)."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "aiops-services" / "remediation"))

os.environ.setdefault("REMEDIATION_DB_PATH", str(Path(tempfile.mkdtemp()) / "r.db"))
os.environ["REMEDIATION_API_KEY"] = "test-secret-key"
os.environ["SIMULATE_ONLY"] = "true"


def test_unknown_action_is_high_risk():
    from app.classifier import classify_action
    from app.models import RiskLevel

    c = classify_action("Do something weird to prod", default_service="checkout-service")
    assert c.risk_level == RiskLevel.HIGH


def test_restart_is_high_risk():
    from app.classifier import classify_action
    from app.models import ActionType, RiskLevel

    c = classify_action("Restart payment-service pods", default_service="payment-service")
    assert c.risk_level == RiskLevel.HIGH
    assert c.action_type == ActionType.RESTART_SERVICE


def test_chaos_reset_is_low_risk():
    from app.classifier import classify_action
    from app.models import ActionType, RiskLevel

    c = classify_action("Reset error_rate chaos on checkout-service")
    assert c.risk_level == RiskLevel.LOW
    assert c.action_type == ActionType.RESET_ERROR_RATE


def test_approve_requires_api_key():
    from fastapi.testclient import TestClient

    from app import main as main_mod

    client = TestClient(main_mod.app)
    # health open
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["details"]["auth_required"] is True

    # approve without key
    r = client.post(
        "/actions/nonexistent/approve",
        json={"executed_by": "t", "execute_now": False},
    )
    assert r.status_code == 401

    # approve with wrong key
    r = client.post(
        "/actions/nonexistent/approve",
        json={"executed_by": "t", "execute_now": False},
        headers={"X-API-Key": "wrong"},
    )
    assert r.status_code == 401

    # with correct key → 404 (auth passed, action missing)
    r = client.post(
        "/actions/nonexistent/approve",
        json={"executed_by": "t", "execute_now": False},
        headers={"X-API-Key": "test-secret-key"},
    )
    assert r.status_code == 404
