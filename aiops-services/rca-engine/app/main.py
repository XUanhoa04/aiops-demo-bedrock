"""
RCA Engine (Day-2 stub).

Production design (to implement next):
- Pull incident + related metrics/logs/traces from LGTM
- Prompt Amazon Bedrock (Claude) for ranked root-cause hypotheses
- Write root_cause + rca_confidence back to incident-manager
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from pydantic import BaseModel

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

setup_logging()

SERVICE_NAME = "aiops-rca-engine"
app = FastAPI(title="AIOps RCA Engine", version="0.1.0-day2-stub")


@app.on_event("startup")
def _startup() -> None:
    setup_otel(SERVICE_NAME, app=app)


class RCARequest(BaseModel):
    incident_id: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        details={
            "mode": "stub",
            "bedrock_model_id": os.getenv("BEDROCK_MODEL_ID", ""),
            "aws_region": os.getenv("AWS_DEFAULT_REGION", ""),
            "bedrock_key_present": bool(os.getenv("AWS_ACCESS_KEY_ID")),
        },
    )


@app.post("/rca/analyze")
def analyze(body: RCARequest) -> dict:
    """Placeholder — returns a deterministic mock hypothesis for pipeline demos."""
    return {
        "incident_id": body.incident_id,
        "status": "not_implemented",
        "message": "Day-2: wire Bedrock Converse API + evidence pack from Prometheus/Loki/Tempo",
        "hypotheses": [
            {
                "rank": 1,
                "cause": "Elevated error_rate on payment-service after chaos injection",
                "confidence": 0.42,
                "evidence": ["stub"],
            }
        ],
    }
