"""Prometheus metrics for Decision Engine routing health."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prometheus_client import Counter, Gauge, Info

if TYPE_CHECKING:
    from app.models import EngineDecision

SERVICE_INFO = Info("aiops_decision_engine", "Decision engine metadata")
SERVICE_INFO.info({"component": "decision-engine", "version": "0.1.0"})

DECISIONS_TOTAL = Counter(
    "decision_engine_decisions_total",
    "Total decisions by action and confidence band",
    ["action", "band"],
)

ESCALATIONS_TOTAL = Counter(
    "decision_engine_escalations_total",
    "Escalations to on-call",
    ["forced"],
)

LLM_CALLS_TOTAL = Counter(
    "decision_engine_llm_calls_total",
    "Bedrock/RCA invocations triggered by Decision Engine (medium path)",
)

REMEDIATION_PROPOSALS_TOTAL = Counter(
    "decision_engine_remediation_proposals_total",
    "Gated remediation proposals from high-confidence path",
)

ITERATIONS_TOTAL = Counter(
    "decision_engine_iterations_total",
    "Iteration steps across all decide() calls",
)

LAST_CONFIDENCE = Gauge(
    "decision_engine_last_confidence",
    "Confidence score of the latest decision",
    ["service", "action"],
)

LAST_BAND = Gauge(
    "decision_engine_band",
    "1 if latest decision for service is this band",
    ["service", "band"],
)


def record_decision(decision: "EngineDecision") -> None:
    LAST_CONFIDENCE.labels(
        service=decision.service_name,
        action=decision.action.value,
    ).set(decision.confidence_score)
    for band in ("high", "medium", "low"):
        LAST_BAND.labels(service=decision.service_name, band=band).set(
            1.0 if decision.band.value == band else 0.0
        )
