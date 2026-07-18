"""
Known remediation patterns for the HIGH-confidence auto path.

Why a static pattern catalog?
-----------------------------
Auto-remediation must be *boring and reversible*. Free-text LLM actions are
not safe to auto-queue even at confidence 99. We only auto-propose when the
anomaly matches a pre-approved playbook (chaos reset, latency clear, …).

Production: store these in a policy DB / OPA; here they are code + env-extensible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class KnownPattern:
    id: str
    description: str
    # Remediation free-text that classifier maps to low-risk ActionType
    remediation_action: str
    # Match helpers
    metric_substrings: tuple[str, ...]
    explanation_regex: Optional[re.Pattern[str]] = None
    min_metric_value: Optional[float] = None

    def matches(
        self,
        *,
        metric_name: str,
        metric_value: float,
        explanation: str,
        service_name: str,
    ) -> bool:
        m = (metric_name or "").lower()
        if not any(s in m for s in self.metric_substrings):
            return False
        if self.min_metric_value is not None and metric_value < self.min_metric_value:
            return False
        if self.explanation_regex and not self.explanation_regex.search(explanation or ""):
            return False
        return True


# Catalog — keep LOW-RISK only (reset chaos / log). Never put restart/scale here.
KNOWN_PATTERNS: list[KnownPattern] = [
    KnownPattern(
        id="error_rate_spike_reset",
        description="Elevated HTTP error rate → reset chaos/error injection",
        remediation_action="reset error_rate / disable chaos on {service}",
        metric_substrings=("error_rate", "http_error"),
        min_metric_value=0.10,
    ),
    KnownPattern(
        id="latency_spike_reset",
        description="Elevated latency p95/p99 → clear extra latency chaos",
        remediation_action="reset latency / clear extra_latency on {service}",
        metric_substrings=("latency", "duration"),
        min_metric_value=0.3,
    ),
    KnownPattern(
        id="chaos_event_reset",
        description="Chaos/fault-inject event in context → disable chaos",
        remediation_action="disable chaos / reset error_rate on {service}",
        metric_substrings=("error_rate", "latency", "request_rate", "multivariate"),
        explanation_regex=re.compile(r"\b(chaos|fault.?inject)\b", re.I),
    ),
]


@dataclass
class PatternMatch:
    pattern: KnownPattern
    action_text: str
    reason: str


def find_known_pattern(
    *,
    service_name: str,
    metric_name: str,
    metric_value: float,
    explanation: str = "",
    events: Optional[list[dict[str, Any]]] = None,
) -> Optional[PatternMatch]:
    """Return the first matching known pattern, or None."""
    events = events or []
    # Boost: if events mention chaos, inject keyword into explanation for regex
    event_blob = " ".join(
        str(e.get("type") or "") + " " + str(e.get("message") or "") for e in events
    )
    combined = f"{explanation} {event_blob}"

    for p in KNOWN_PATTERNS:
        if p.matches(
            metric_name=metric_name,
            metric_value=metric_value,
            explanation=combined,
            service_name=service_name,
        ):
            action = p.remediation_action.format(service=service_name)
            return PatternMatch(
                pattern=p,
                action_text=action,
                reason=f"matched known pattern id={p.id}: {p.description}",
            )
    return None
