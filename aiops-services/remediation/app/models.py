"""Remediation domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RiskLevel(str, Enum):
    LOW = "low"
    HIGH = "high"


class ActionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTED = "executed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"
    SIMULATED = "simulated"


class ActionType(str, Enum):
    RESET_ERROR_RATE = "reset_error_rate"
    RESET_LATENCY = "reset_latency"
    RESTART_SERVICE = "restart_service"
    SCALE_DEPLOYMENT = "scale_deployment"
    MARK_FALSE_POSITIVE = "mark_false_positive"
    LOG_ONLY = "log_only"
    CUSTOM = "custom"


class ActionRecord(BaseModel):
    """Persisted remediation action history row."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    incident_id: str
    action_type: str
    action_text: str = ""
    target_service: str = ""
    risk_level: RiskLevel = RiskLevel.HIGH
    status: ActionStatus = ActionStatus.PROPOSED
    payload: dict[str, Any] = Field(default_factory=dict)
    result: Optional[str] = None
    executed_by: Optional[str] = None
    command: Optional[str] = None  # docker/kubectl command that was (or would be) run
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    executed_at: Optional[datetime] = None


class ProposeRequest(BaseModel):
    incident_id: str
    # If empty, pull suggested_actions from incident.remediation_notes / RCA
    actions: list[str] = Field(default_factory=list)
    auto_execute_low_risk: Optional[bool] = None


class ExecuteRequest(BaseModel):
    executed_by: str = "operator"
    force: bool = Field(
        default=False,
        description="Allow execute without prior approval (ops override)",
    )


class ApproveRequest(BaseModel):
    executed_by: str = "operator"
    execute_now: bool = True


class FalsePositiveRequest(BaseModel):
    executed_by: str = "operator"
    note: str = "Marked as false positive from remediation UI"


class IncidentBundle(BaseModel):
    """UI-friendly incident + RCA + proposed actions."""

    incident: dict[str, Any]
    suggested_actions: list[str] = Field(default_factory=list)
    rca: dict[str, Any] = Field(default_factory=dict)
    history: list[ActionRecord] = Field(default_factory=list)
