"""
Feedback collector (Day-2 stub).

Closes the AIOps loop: SRE marks RCA true/false positive → future model fine-tuning
or prompt/rule tuning. For the demo we PATCH incident-manager.human_feedback.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

setup_logging()

SERVICE_NAME = "aiops-feedback-collector"
INCIDENT_MANAGER_URL = os.getenv(
    "INCIDENT_MANAGER_URL", "http://aiops-incident-manager:8002"
)

app = FastAPI(title="AIOps Feedback Collector", version="0.1.0-day2-stub")


@app.on_event("startup")
def _startup() -> None:
    setup_otel(SERVICE_NAME, app=app)


class FeedbackRequest(BaseModel):
    incident_id: str
    useful: bool = True
    comment: str = Field(default="", max_length=2000)
    correct_root_cause: str | None = None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        details={"mode": "stub", "incident_manager": INCIDENT_MANAGER_URL},
    )


@app.post("/feedback")
def submit_feedback(body: FeedbackRequest) -> dict:
    label = "useful" if body.useful else "not_useful"
    text = f"[{label}] {body.comment}".strip()
    if body.correct_root_cause:
        text = f"{text} | corrected_rca={body.correct_root_cause}"

    payload = {
        "human_feedback": text,
        "status": "resolved" if body.useful else "false_positive",
    }
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.patch(
                f"{INCIDENT_MANAGER_URL.rstrip('/')}/incidents/{body.incident_id}",
                json=payload,
            )
            if resp.status_code == 404:
                raise HTTPException(status_code=404, detail="incident not found")
            resp.raise_for_status()
            return {"ok": True, "incident": resp.json()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
