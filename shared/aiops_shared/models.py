"""
Shared domain models (Pydantic).

Production choice: schema-first events between services so Redis payloads stay
versionable. In a real platform these would live in a protobuf/Avro schema registry.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AnomalySeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentStatus(str, Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    REMEDIATING = "remediating"
    RESOLVED = "resolved"
    CLOSED = "closed"
    FALSE_POSITIVE = "false_positive"


class AnomalyEvent(BaseModel):
    """Emitted by anomaly-detector → Redis → consumed by incident-manager."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    service_name: str
    metric_name: str
    metric_value: float
    threshold: float
    severity: AnomalySeverity = AnomalySeverity.MEDIUM
    detector: str = "threshold"  # threshold | zscore | composite
    message: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=utc_now)
    schema_version: str = "1.0"

    def to_redis_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_redis_json(cls, raw: str | bytes) -> "AnomalyEvent":
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return cls.model_validate_json(raw)


class Incident(BaseModel):
    """Persisted incident ticket (SQLite in demo; Postgres/Jira in production)."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    description: str = ""
    status: IncidentStatus = IncidentStatus.OPEN
    severity: AnomalySeverity = AnomalySeverity.MEDIUM
    service_name: str
    source_anomaly_id: Optional[str] = None
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    threshold: Optional[float] = None
    labels: dict[str, str] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    # RCA / remediation / human feedback
    root_cause: Optional[str] = None
    rca_confidence: Optional[float] = None
    remediation_notes: Optional[str] = None
    human_feedback: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    resolved_at: Optional[datetime] = None

    def touch(self) -> None:
        self.updated_at = utc_now()


class RemediationAction(BaseModel):
    """Planned or executed remediation step."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    incident_id: str
    action_type: str  # e.g. reset_error_rate | scale_hint | restart_hint
    target_service: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "proposed"  # proposed | executed | failed | skipped
    result: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str
    version: str = "0.1.0"
    details: dict[str, Any] = Field(default_factory=dict)
