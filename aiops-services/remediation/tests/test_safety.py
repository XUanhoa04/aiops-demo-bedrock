"""Safety regression tests: resource serialization and rollback."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import ActionRepository
from app.executor import ActionExecutor
from app.models import ActionRecord, ActionStatus, ActionType, RiskLevel, utc_now
from app.service import RemediationService


class _Incidents:
    def close(self):
        pass

    def get_incident(self, incident_id):
        return {"id": incident_id, "remediation_notes": ""}

    def patch_incident(self, incident_id, body):
        return {"id": incident_id, **body}


class _Executor:
    def __init__(self):
        self.calls = 0

    def close(self):
        pass

    def execute(self, rec, *, executed_by):
        self.calls += 1
        rec.executed_by = executed_by
        rec.status = ActionStatus.EXECUTED
        rec.executed_at = utc_now()
        rec.result = "done"
        return rec


def test_resource_lock_blocks_conflicting_service_action():
    db_path = str(Path(tempfile.mkdtemp(prefix="rem-lock-")) / "actions.db")
    repo = ActionRepository(db_path=db_path)
    executor = _Executor()
    service = RemediationService(repo=repo, incidents=_Incidents(), executor=executor)
    rec = ActionRecord(
        incident_id="inc-1",
        action_type=ActionType.RESET_ERROR_RATE.value,
        action_text="reset",
        target_service="checkout-service",
        risk_level=RiskLevel.LOW,
    )
    repo.insert(rec)
    acquired, _ = repo.try_acquire_resource_lock(
        "service:checkout-service", "other-action", 60
    )
    assert acquired

    blocked = service.execute(rec.id, executed_by="operator")
    assert blocked.status == ActionStatus.PROPOSED
    assert "locked by action other-action" in (blocked.result or "")
    assert executor.calls == 0

    repo.release_resource_lock("service:checkout-service", "other-action")
    executed = service.execute(rec.id, executed_by="operator")
    assert executed.status == ActionStatus.EXECUTED
    assert executor.calls == 1


class _Response:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._body


class _RollbackHTTP:
    def __init__(self):
        self.posts: list[dict] = []

    def get(self, url):
        if url.endswith("/chaos") and not self.posts:
            return _Response(
                200,
                {
                    "error_rate": 0.8,
                    "base_latency_ms": 50.0,
                    "extra_latency_ms": 500.0,
                    "fault_mode": "dependency_timeout",
                },
            )
        if url.endswith("/health"):
            return _Response(503, {"status": "degraded"})
        return _Response(200, {"error_rate": 0.01, "extra_latency_ms": 0})

    def post(self, url, json):
        self.posts.append(dict(json))
        return _Response(200, dict(json))

    def close(self):
        pass


def test_failed_post_action_health_check_rolls_back_previous_state():
    executor = ActionExecutor()
    executor._http.close()
    fake_http = _RollbackHTTP()
    executor._http = fake_http
    previous_verify = settings.verify_after_execute
    previous_simulate = settings.simulate_only
    settings.verify_after_execute = True
    settings.simulate_only = False
    try:
        rec = ActionRecord(
            incident_id="inc-rollback",
            action_type=ActionType.RESET_ERROR_RATE.value,
            target_service="checkout-service",
            risk_level=RiskLevel.LOW,
        )
        result = executor.execute(rec, executed_by="operator")
    finally:
        settings.verify_after_execute = previous_verify
        settings.simulate_only = previous_simulate
        executor.close()

    assert result.status == ActionStatus.ROLLED_BACK
    assert result.verification["ok"] is False
    assert result.rollback["ok"] is True
    assert len(fake_http.posts) == 2
    assert fake_http.posts[-1]["error_rate"] == 0.8
    assert fake_http.posts[-1]["fault_mode"] == "dependency_timeout"
