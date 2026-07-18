"""
Structured output models for anomaly-detector → Decision Engine.

Why a dedicated decision object?
--------------------------------
Downstream (incident-manager, RCA, remediation / Decision Engine) should not
parse free-text alert strings. They need a *versioned, typed* payload with:

* algorithmic signals (score, method, explanation)
* multi-signal context (metrics / logs / traces / events)
* confidence_score + breakdown so auto-remediation can gate on trust

Schema stays JSON-serializable so Redis / webhooks / FastAPI share one shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MethodDetail(BaseModel):
    """One detector algorithm's vote on a single observation."""

    method: str
    score: float
    is_anomaly: bool
    explanation: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)


class ContextCompleteness(BaseModel):
    """
    Binary checklist the Decision Engine can branch on.

    Completeness is not the same as confidence: you can have full context and
    still a weak anomaly (low score), or a strong metric spike with zero traces.
    """

    has_trace_id: bool = False
    has_related_logs: bool = False
    has_sufficient_metrics: bool = False
    has_events: bool = False
    # 0.0–1.0 fraction of expected signal families present
    ratio: float = 0.0
    # Human-readable missing pieces (also mirrored on confidence result)
    missing: list[str] = Field(default_factory=list)


class SignalBundle(BaseModel):
    """Multi-signal snapshot gathered around the anomaly window."""

    metrics: dict[str, Any] = Field(default_factory=dict)
    logs: list[dict[str, Any]] = Field(default_factory=list)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    primary_trace_id: Optional[str] = None
    sources_ok: dict[str, bool] = Field(default_factory=dict)
    gather_errors: list[str] = Field(default_factory=list)
    window_start_iso: Optional[str] = None
    window_end_iso: Optional[str] = None
    completeness: ContextCompleteness = Field(default_factory=ContextCompleteness)


class ConfidenceBreakdown(BaseModel):
    """
    Per-signal contribution (points toward the 0–100 score) *before* penalties,
    plus applied penalties. Weights documented in confidence_scorer.py.
    """

    # Weighted points actually earned (after quality scaling, before penalties)
    metrics: float = 0.0
    traces: float = 0.0
    logs: float = 0.0
    events: float = 0.0
    # Algorithm strength add-on (capped) — how "loud" the detectors were
    algorithm_strength: float = 0.0
    # Subtracted points (missing context, source down, weak signals)
    penalties: float = 0.0
    penalty_reasons: list[str] = Field(default_factory=list)
    # Configured weights used for this computation (for audit / tuning UI)
    weights: dict[str, float] = Field(default_factory=dict)


class ConfidenceResult(BaseModel):
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=100.0,
        description="0–100 trust that this is a real, actionable anomaly",
    )
    confidence_breakdown: ConfidenceBreakdown = Field(
        default_factory=ConfidenceBreakdown
    )
    missing_context: list[str] = Field(default_factory=list)


class DetectionDecision(BaseModel):
    """
    Canonical object for the Decision Engine.

    Fields of record:
      - anomaly_score / detection_method / explanation  → algorithmic layer
      - context + completeness                           → multi-signal layer
      - confidence_score / breakdown / missing_context   → scoring engine
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    service_name: str
    metric_name: str
    metric_value: float
    is_anomaly: bool
    anomaly_score: float
    detection_method: str  # primary / winning method(s) joined
    detection_methods: list[str] = Field(default_factory=list)
    explanation: str = ""
    severity: str = "medium"
    threshold: float = 0.0
    method_details: list[MethodDetail] = Field(default_factory=list)
    features: dict[str, float] = Field(default_factory=dict)

    # Multi-signal + completeness
    context: SignalBundle = Field(default_factory=SignalBundle)
    context_completeness: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="0–1 fraction of signal families present",
    )

    # Confidence engine
    confidence_score: float = Field(0.0, ge=0.0, le=100.0)
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    missing_context: list[str] = Field(default_factory=list)

    labels: dict[str, str] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=utc_now)
    schema_version: str = "2.0"

    def to_decision_dict(self) -> dict[str, Any]:
        """Stable dict shape for Decision Engine / webhook consumers."""
        return self.model_dump(mode="json")
