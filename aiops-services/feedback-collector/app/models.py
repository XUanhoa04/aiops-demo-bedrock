"""Feedback domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Thumb(str):
    """Boolean-friendly vote: True = thumbs up, False = thumbs down."""


class FeedbackCreate(BaseModel):
    incident_id: str
    # Thumbs: True = 👍, False = 👎, None = skipped
    anomaly_correct: Optional[bool] = Field(
        default=None,
        description="Was the anomaly detection correct? (not a false positive)",
    )
    rca_useful: Optional[bool] = Field(
        default=None,
        description="Was the RCA root-cause helpful?",
    )
    action_effective: Optional[bool] = Field(
        default=None,
        description="Did remediation / suggested actions help?",
    )
    comment: str = Field(default="", max_length=4000)
    reviewer: str = Field(default="oncall", max_length=128)
    # Optional corrected RCA text for training / prompt tuning
    corrected_root_cause: Optional[str] = Field(default=None, max_length=2000)


class FeedbackRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    incident_id: str
    anomaly_correct: Optional[bool] = None
    rca_useful: Optional[bool] = None
    action_effective: Optional[bool] = None
    comment: str = ""
    reviewer: str = "oncall"
    corrected_root_cause: Optional[str] = None
    # Snapshot fields from incident at review time
    service_name: Optional[str] = None
    severity: Optional[str] = None
    incident_status: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)

    @property
    def is_false_positive(self) -> bool:
        """FP if reviewer explicitly said anomaly was NOT correct."""
        return self.anomaly_correct is False


class FeedbackStats(BaseModel):
    total: int = 0
    with_anomaly_vote: int = 0
    with_rca_vote: int = 0
    with_action_vote: int = 0
    anomaly_positive: int = 0
    rca_positive: int = 0
    action_positive: int = 0
    false_positive_count: int = 0
    feedback_positive_rate: float = 0.0  # 0–1 across all cast thumbs
    rca_accuracy_estimate: float = 0.0  # 0–1 from rca_useful ups
    anomaly_precision_estimate: float = 0.0  # 0–1 from anomaly_correct ups
    action_success_rate: float = 0.0


class TuningSuggestion(BaseModel):
    false_positive_count: int
    anomaly_votes: int
    false_positive_rate: float
    recommendation: str
    suggested_zscore_threshold: Optional[float] = None
    suggested_error_rate_threshold: Optional[float] = None
    current_zscore_threshold: float
    current_error_rate_threshold: float
    details: list[str] = Field(default_factory=list)
    sample_fp_services: list[str] = Field(default_factory=list)
