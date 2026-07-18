"""
Remediation service (Day-2 stub).

Safe demos: call checkout/payment /chaos to reset error rates (not real k8s restarts).
Production: runbooks via Ansible/SSM, feature flags, auto-scaling hooks, change freezes.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse, RemediationAction
from aiops_shared.otel import setup_otel

setup_logging()

SERVICE_NAME = "aiops-remediation"
CHECKOUT_URL = os.getenv("CHECKOUT_URL", "http://checkout-service:8080")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8081")

app = FastAPI(title="AIOps Remediation", version="0.1.0-day2-stub")


@app.on_event("startup")
def _startup() -> None:
    setup_otel(SERVICE_NAME, app=app)


class RemediateRequest(BaseModel):
    incident_id: str
    action_type: str = Field(
        default="reset_error_rate",
        description="reset_error_rate | reset_latency",
    )
    target_service: str = Field(default="checkout-service")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        details={"mode": "stub", "checkout_url": CHECKOUT_URL},
    )


@app.post("/remediate", response_model=RemediationAction)
def remediate(body: RemediateRequest) -> RemediationAction:
    action = RemediationAction(
        incident_id=body.incident_id,
        action_type=body.action_type,
        target_service=body.target_service,
        payload={},
        status="proposed",
    )
    base = CHECKOUT_URL if "checkout" in body.target_service else PAYMENT_URL
    try:
        if body.action_type == "reset_error_rate":
            payload = {"error_rate": 0.01, "extra_latency_ms": 0}
        elif body.action_type == "reset_latency":
            payload = {"extra_latency_ms": 0, "base_latency_ms": 50}
        else:
            action.status = "skipped"
            action.result = f"unknown action_type={body.action_type}"
            return action

        with httpx.Client(timeout=5.0) as client:
            resp = client.post(f"{base.rstrip('/')}/chaos", json=payload)
            action.payload = payload
            if resp.is_success:
                action.status = "executed"
                action.result = resp.text
            else:
                action.status = "failed"
                action.result = f"HTTP {resp.status_code}: {resp.text}"
    except Exception as exc:
        action.status = "failed"
        action.result = str(exc)
    return action
