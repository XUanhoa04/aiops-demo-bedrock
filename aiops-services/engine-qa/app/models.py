"""
Engine QA domain models.

On-call answers four questions that map to engine/LLM layers:

  1. anomaly_correct?     → detector precision / FP
  2. confidence_reasonable? → confidence scorer calibration
  3. rca_useful?          → LLM/RCA quality (+ hallucination if not useful
                             *and* corrected_root_cause provided)
  4. decision_correct?    → decision-engine routing quality

Aggregates feed precision/recall *estimates*, FP rate, hallucination rate,
avg decision iterations, and tuning suggestions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class QAReviewCreate(BaseModel):
    """Submit a meta-review for one incident / anomaly / decision."""

    incident_id: str = Field(..., examples=["3c5d0f7f-…"])
    anomaly_id: Optional[str] = None
    decision_id: Optional[str] = None

    # --- Four core thumbs (None = skipped) ---
    anomaly_correct: Optional[bool] = Field(
        default=None,
        description="Was the anomaly a true positive?",
    )
    confidence_reasonable: Optional[bool] = Field(
        default=None,
        description="Was confidence_score calibrated (not wildly over/under)?",
    )
    rca_useful: Optional[bool] = Field(
        default=None,
        description="Was RCA / LLM root-cause helpful?",
    )
    decision_correct: Optional[bool] = Field(
        default=None,
        description="Was Decision Engine routing (auto/RCA/escalate) right?",
    )

    # Optional numeric judgments
    expected_confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=100.0,
        description="What confidence would on-call have assigned?",
    )
    # If RCA was wrong / hallucinated, free-text ground truth
    corrected_root_cause: Optional[str] = Field(default=None, max_length=2000)
    llm_hallucinated: Optional[bool] = Field(
        default=None,
        description="Explicit: did the LLM invent evidence / root cause?",
    )

    # Decision / loop context (can be filled by client snapshot)
    decision_action: Optional[str] = None
    decision_iterations: Optional[int] = Field(default=None, ge=0, le=20)
    engine_confidence: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    llm_confidence: Optional[float] = Field(default=None, ge=0.0, le=100.0)

    comment: str = Field(default="", max_length=4000)
    reviewer: str = Field(default="oncall-sre", max_length=128)


class QAReview(QAReviewCreate):
    id: str = Field(default_factory=lambda: str(uuid4()))
    # Snapshots at review time
    service_name: Optional[str] = None
    severity: Optional[str] = None
    metric_name: Optional[str] = None
    detection_method: Optional[str] = None
    missing_context: list[str] = Field(default_factory=list)
    confidence_breakdown: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def is_false_positive(self) -> bool:
        return self.anomaly_correct is False

    @property
    def is_hallucination(self) -> bool:
        if self.llm_hallucinated is True:
            return True
        # Proxy: RCA not useful AND reviewer provided a correction
        if self.rca_useful is False and (self.corrected_root_cause or "").strip():
            return True
        return False


class EngineQualityMetrics(BaseModel):
    """
    Aggregate quality of AIOps Engine + LLM.

    All rates are 0–1. Counts are absolute. Estimates are labeled as such
    because we lack full ground-truth labeling of every non-alerted minute
    (classic ops labeling problem → precision is better estimated than recall).
    """

    total_reviews: int = 0

    # Anomaly / detector
    anomaly_votes: int = 0
    anomaly_true_positive: int = 0
    anomaly_false_positive: int = 0
    precision_estimate: float = 0.0  # TP / (TP+FP) from anomaly votes
    # Recall proxy: among reviews where severity high/critical and anomaly voted,
    # share marked correct. Not true recall (no FN from silent failures).
    recall_estimate: float = 0.0
    false_positive_rate: float = 0.0  # FP / anomaly_votes

    # Confidence scorer calibration
    confidence_votes: int = 0
    confidence_reasonable_rate: float = 0.0
    overconfidence_count: int = 0  # high engine conf + FP
    mean_engine_confidence: float = 0.0
    mean_expected_confidence: float = 0.0
    mean_confidence_error: float = 0.0  # |engine - expected|

    # RCA / LLM
    rca_votes: int = 0
    rca_useful_rate: float = 0.0
    hallucination_count: int = 0
    hallucination_rate: float = 0.0  # among rca votes (or explicit labels)
    mean_llm_confidence: float = 0.0

    # Decision engine
    decision_votes: int = 0
    decision_correct_rate: float = 0.0
    mean_decision_iterations: float = 0.0
    handoff_reviews: int = 0  # decision_action escalate / handoff

    # Composite
    overall_engine_health: float = 0.0  # 0–1 blended score for dashboards
    notes: list[str] = Field(default_factory=list)


class WeightSuggestion(BaseModel):
    metrics: float
    traces: float
    logs: float
    events: float


class TuningAdvice(BaseModel):
    """Suggested knob changes — never auto-applied."""

    sample_size: int
    false_positive_rate: float
    hallucination_rate: float
    decision_error_rate: float
    confidence_reasonable_rate: float

    recommendation: str
    details: list[str] = Field(default_factory=list)

    # Detector thresholds
    suggested_zscore_threshold: Optional[float] = None
    suggested_error_rate_threshold: Optional[float] = None
    current_zscore_threshold: float = 2.5
    current_error_rate_threshold: float = 0.15

    # Confidence scorer weights (normalized)
    suggested_confidence_weights: Optional[WeightSuggestion] = None
    current_confidence_weights: WeightSuggestion = Field(
        default_factory=lambda: WeightSuggestion(
            metrics=0.4, traces=0.3, logs=0.2, events=0.1
        )
    )

    # Decision bands
    suggested_confidence_high: Optional[float] = None
    suggested_confidence_medium: Optional[float] = None
    current_confidence_high: float = 85.0
    current_confidence_medium: float = 60.0

    env_snippet: str = ""  # copy-paste ready .env deltas


class QADashboard(BaseModel):
    quality: EngineQualityMetrics
    tuning: TuningAdvice
    recent_reviews: list[QAReview] = Field(default_factory=list)
    pipeline_status: dict[str, bool] = Field(default_factory=dict)
