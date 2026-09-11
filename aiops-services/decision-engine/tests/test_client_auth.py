"""Test that DecisionEngine ServiceClients forwards X-API-Key to remediation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.clients import ServiceClients
from app.config import settings


def test_propose_remediation_forwards_api_key_header() -> None:
    clients = ServiceClients()
    try:
        with patch.object(clients._http, "post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = [{"id": "act-1", "status": "proposed"}]
            mock_post.return_value = mock_resp

            prev_key = settings.remediation_api_key
            settings.remediation_api_key = "test-secret-token"
            try:
                res = clients.propose_remediation(
                    incident_id="inc-123",
                    actions=["reset error_rate on checkout-service"],
                    auto_execute_low_risk=False,
                )
                assert res is not None
                assert len(res) == 1
                mock_post.assert_called_once()
                call_kwargs = mock_post.call_args.kwargs
                assert call_kwargs.get("headers") == {"X-API-Key": "test-secret-token"}
            finally:
                settings.remediation_api_key = prev_key
    finally:
        clients.close()


def test_patch_incident_sends_context_merge() -> None:
    clients = ServiceClients()
    try:
        with patch.object(clients._http, "patch") as mock_patch:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"id": "inc-456"}
            mock_patch.return_value = mock_resp

            ok = clients.patch_incident(
                incident_id="inc-456",
                status="investigating",
                context_merge={"decision_action": "rca_suggest", "confidence_score": 85.0},
            )
            assert ok is True
            mock_patch.assert_called_once()
            call_kwargs = mock_patch.call_args.kwargs
            assert "json" in call_kwargs
            payload = call_kwargs["json"]
            assert payload["status"] == "investigating"
            assert payload["context"] == {
                "decision_action": "rca_suggest",
                "confidence_score": 85.0,
            }
    finally:
        clients.close()

