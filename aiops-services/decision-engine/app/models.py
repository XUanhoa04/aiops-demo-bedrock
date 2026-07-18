"""
Request / response schemas for the Decision Engine.

`EngineDecision` is the object the rest of the platform (console, audit, IM)
should consume — every branch records *why* we chose it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from app.decision_table import ConfidenceBand, DecisionAction


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DecideRequest(BaseModel):
    """
    Input from anomaly-detector DetectionDecision, AnomalyEvent.context, or manual.

    Prefer full DetectionDecision fields when available.
    """

    # Identity
    anomaly_id: Optional[str] = None
    incident_id: Optional[str] = None
    service_name: str = Field(..., examples=["checkout-service"])
    metric_name: str = Field(default="http_error_rate")
    metric_value: float = 0.0
    anomaly_score: float = 0.0
    detection_method: str = ""
    explanation: str = ""
    severity: str = "medium"

    # Confidence Scorer outputs (required for policy)
    confidence_score: float = Field(..., ge=0.0, le=100.0, examples=[72.0])
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    missing_context: list[str] = Field(default_factory=list)
    context_completeness: float = Field(default=0.0, ge=0.0, le=1.0)

    # Multi-signal snapshot (optional; used for pattern match + enrich)
    signals: dict[str, Any] = Field(default_factory=dict)
    primary_trace_id: Optional[str] = None
    features: dict[str, float] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)

    # Control
    force_action: Optional[DecisionAction] = None
    skip_side_effects: bool = Field(
        default=False,
        description="If true, only compute decision (no RCA/remediate/IM calls)",
    )


class IterationRecord(BaseModel):
    iteration: int
    action: DecisionAction
    band: ConfidenceBand
    confidence_score: float
    reason: str
    llm_called: bool = False
    llm_confidence: Optional[float] = None
    enrichment: dict[str, Any] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class EngineDecision(BaseModel):
    """
    Final (or intermediate) decision object for audit + downstream routing.
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    service_name: str
    metric_name: str = ""
    incident_id: Optional[str] = None
    anomaly_id: Optional[str] = None

    # Policy outcome
    action: DecisionAction
    band: ConfidenceBand
    confidence_score: float
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    missing_context: list[str] = Field(default_factory=list)
    context_completeness: float = 0.0

    # Explainability (always filled)
    reason: str = ""
    decision_trace: list[str] = Field(
        default_factory=list,
        description="Ordered human-readable steps: why this action?",
    )
    iterations: list[IterationRecord] = Field(default_factory=list)
    iteration_count: int = 0

    # Pattern / remediation
    known_pattern_id: Optional[str] = None
    proposed_actions: list[str] = Field(default_factory=list)
    remediation_result: Optional[dict[str, Any]] = None

    # LLM / RCA (MEDIUM path)
    llm_called: bool = False
    llm_confidence: Optional[float] = None
    rca_result: Optional[dict[str, Any]] = None
    suggestions: list[str] = Field(default_factory=list)

    # Escalation
    escalated: bool = False
    escalate_reason: Optional[str] = None

    # Side-effect flags
    side_effects_skipped: bool = False
    incident_patched: bool = False

    created_at: datetime = Field(default_factory=utc_now)
    schema_version: str = "1.0"

    def to_incident_context(self) -> dict[str, Any]:
        """Compact blob to merge into incident.context for UI/audit."""
        return {
            "decision_engine": {
                "id": self.id,
                "action": self.action.value,
                "band": self.band.value,
                "confidence_score": self.confidence_score,
                "confidence_breakdown": self.confidence_breakdown,
                "missing_context": self.missing_context,
                "reason": self.reason,
                "decision_trace": self.decision_trace,
                "iteration_count": self.iteration_count,
                "known_pattern_id": self.known_pattern_id,
                "llm_called": self.llm_called,
                "llm_confidence": self.llm_confidence,
                "suggestions": self.suggestions,
                "escalated": self.escalated,
                "escalate_reason": self.escalate_reason,
                "proposed_actions": self.proposed_actions,
            }
        }


class AnomalyEventIn(BaseModel):
    """Subset of shared AnomalyEvent for Redis / webhook ingest."""

    id: str = ""
    service_name: str
    metric_name: str = "http_error_rate"
    metric_value: float = 0.0
    threshold: float = 0.0
    severity: str = "medium"
    detector: str = ""
    message: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    detected_at: Optional[str] = None


def anomaly_event_to_request(
    event: AnomalyEventIn,
    *,
    incident_id: Optional[str] = None,
) -> DecideRequest:
    ctx = event.context or {}
    signals = ctx.get("signals") or {}
    return DecideRequest(
        anomaly_id=event.id or None,
        incident_id=incident_id,
        service_name=event.service_name,
        metric_name=event.metric_name,
        metric_value=event.metric_value,
        anomaly_score=float(ctx.get("anomaly_score") or 0.0),
        detection_method=str(
            ctx.get("detection_method")
            or event.labels.get("detection_method")
            or event.detector
            or ""
        ),
        explanation=str(ctx.get("explanation") or event.message or ""),
        severity=event.severity if isinstance(event.severity, str) else str(event.severity),
        confidence_score=float(ctx.get("confidence_score") or 0.0),
        confidence_breakdown=dict(ctx.get("confidence_breakdown") or {}),
        missing_context=list(ctx.get("missing_context") or []),
        context_completeness=float(ctx.get("context_completeness") or 0.0),
        signals=signals if isinstance(signals, dict) else {},
        primary_trace_id=ctx.get("primary_trace_id"),
        features=dict(ctx.get("features") or {}),
        labels=dict(event.labels or {}),
    )
