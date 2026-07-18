"""
Classify remediation suggestions into low-risk (auto) vs high-risk (approval).

Production note
---------------
Risk gates are the difference between a helpful bot and an outage amplifier.
Never auto-run restart/scale/rollback in prod without change-window + approval
workflows (ServiceNow / Slack interactive / policy engine).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.models import ActionType, RiskLevel


@dataclass
class ClassifiedAction:
    action_type: ActionType
    risk_level: RiskLevel
    target_service: str
    action_text: str
    replicas: Optional[int] = None
    reason: str = ""


# High-risk keywords → require human approval
_HIGH_PATTERNS: list[tuple[re.Pattern[str], ActionType]] = [
    (re.compile(r"\b(restart|reboot|kill|recycle)\b", re.I), ActionType.RESTART_SERVICE),
    (re.compile(r"\b(scale|replicas?|hpa|autoscale)\b", re.I), ActionType.SCALE_DEPLOYMENT),
    (re.compile(r"\b(rollback|roll\s*back|undeploy|delete\s+pod)\b", re.I), ActionType.CUSTOM),
    (re.compile(r"\b(drain|cordon|terminate\s+instance)\b", re.I), ActionType.CUSTOM),
]

# Low-risk / demo-safe
_LOW_PATTERNS: list[tuple[re.Pattern[str], ActionType]] = [
    (re.compile(r"\b(reset|clear).{0,20}(error[_\s-]?rate|chaos)\b", re.I), ActionType.RESET_ERROR_RATE),
    (re.compile(r"\b(reset|clear).{0,20}(latency|extra_latency)\b", re.I), ActionType.RESET_LATENCY),
    (re.compile(r"\b(disable|turn\s*off).{0,15}chaos\b", re.I), ActionType.RESET_ERROR_RATE),
    (re.compile(r"\b(check|inspect|verify|grep|open\s+grafana|view\s+logs)\b", re.I), ActionType.LOG_ONLY),
    (re.compile(r"\b(investigate|review|monitor)\b", re.I), ActionType.LOG_ONLY),
]

_SERVICE_RE = re.compile(
    r"\b(checkout-service|payment-service|checkout|payment|aiops-[\w-]+)\b",
    re.I,
)
_REPLICAS_RE = re.compile(r"\b(?:to\s+|replicas?\s*[=:]?\s*)(\d{1,3})\b", re.I)


def _normalize_service(raw: Optional[str], fallback: str = "") -> str:
    if not raw:
        return fallback or "unknown"
    s = raw.lower().strip()
    if s in ("checkout", "checkout-service", "aiops-checkout"):
        return "checkout-service"
    if s in ("payment", "payment-service", "aiops-payment"):
        return "payment-service"
    return s


def classify_action(
    text: str,
    *,
    default_service: str = "",
) -> ClassifiedAction:
    """Map free-text suggested_action → typed action + risk."""
    t = (text or "").strip()
    svc_match = _SERVICE_RE.search(t)
    service = _normalize_service(
        svc_match.group(1) if svc_match else None,
        fallback=default_service,
    )

    replicas = None
    m_rep = _REPLICAS_RE.search(t)
    if m_rep:
        try:
            replicas = int(m_rep.group(1))
        except ValueError:
            replicas = None

    for pat, atype in _HIGH_PATTERNS:
        if pat.search(t):
            return ClassifiedAction(
                action_type=atype,
                risk_level=RiskLevel.HIGH,
                target_service=service,
                action_text=t,
                replicas=replicas,
                reason=f"matched high-risk pattern → {atype.value}",
            )

    for pat, atype in _LOW_PATTERNS:
        if pat.search(t):
            return ClassifiedAction(
                action_type=atype,
                risk_level=RiskLevel.LOW,
                target_service=service,
                action_text=t,
                replicas=replicas,
                reason=f"matched low-risk pattern → {atype.value}",
            )

    # Default: treat unknown operational suggestions as high-risk
    return ClassifiedAction(
        action_type=ActionType.CUSTOM,
        risk_level=RiskLevel.HIGH,
        target_service=service,
        action_text=t,
        replicas=replicas,
        reason="unknown action text — default high-risk (approval required)",
    )


def classify_many(
    actions: list[str],
    *,
    default_service: str = "",
) -> list[ClassifiedAction]:
    return [classify_action(a, default_service=default_service) for a in actions if a and a.strip()]
