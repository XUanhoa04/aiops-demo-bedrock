"""
Decision Table — single source of truth for routing policy.

┌─────────────────────┬──────────────────────────┬─────────────────────────────┐
│ Condition           │ Action                   │ Why                         │
├─────────────────────┼──────────────────────────┼─────────────────────────────┤
│ conf ≥ HIGH (85)    │ AUTO_REMEDIATE_GATED     │ High multi-signal trust +   │
│ AND known pattern   │ (log + propose only;     │ known safe runbook → can    │
│                     │  no force-execute)       │ queue low-risk fix safely   │
├─────────────────────┼──────────────────────────┼─────────────────────────────┤
│ conf ≥ HIGH         │ RCA_SUGGEST (or ESCALATE │ Strong signal but no canned │
│ AND no known pattern│  if LLM disabled)        │ pattern — need analysis     │
├─────────────────────┼──────────────────────────┼─────────────────────────────┤
│ MEDIUM ≤ conf < HIGH│ RCA_SUGGEST              │ Ambiguous: call Bedrock     │
│ (60–85)             │ (LLM, limited tokens)    │ once with enriched context  │
├─────────────────────┼──────────────────────────┼─────────────────────────────┤
│ conf < MEDIUM (60)  │ ESCALATE_ONCALL          │ Too uncertain for automation│
│ OR critical missing │ + explain why            │ / LLM cost not justified    │
│ context             │                          │                             │
├─────────────────────┼──────────────────────────┼─────────────────────────────┤
│ iteration exhausted │ ESCALATE_ONCALL          │ Hard stop: never loop forever│
│ (max 2–3 rounds)    │ (forced handoff)         │                             │
└─────────────────────┴──────────────────────────┴─────────────────────────────┘

Thresholds are Settings (CONFIDENCE_HIGH / CONFIDENCE_MEDIUM) so demos and
prod can diverge without code changes.

LLM cost control
----------------
Bedrock is **only** invoked on the MEDIUM band (and HIGH-without-pattern if
you keep enable_llm). LOW always escalates; HIGH-with-pattern never needs an
LLM to pick `reset_error_rate`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from app.config import settings


class DecisionAction(str, Enum):
    """Machine-readable action the engine will attempt to execute."""

    AUTO_REMEDIATE_GATED = "auto_remediate_gated"
    RCA_SUGGEST = "rca_suggest"
    ESCALATE_ONCALL = "escalate_oncall"
    # Intermediate: gather more multi-signal context then re-score policy
    ENRICH_CONTEXT = "enrich_context"
    # Terminal after max iterations
    HANDOFF_EXHAUSTED = "handoff_exhausted"


class ConfidenceBand(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class PolicyRow:
    """One row of the decision table (for docs + runtime)."""

    band: ConfidenceBand
    requires_known_pattern: Optional[bool]  # None = N/A
    critical_context_ok: bool
    action: DecisionAction
    llm: bool
    summary: str


# Explicit table used by README generation and unit tests.
DECISION_TABLE: list[PolicyRow] = [
    PolicyRow(
        band=ConfidenceBand.HIGH,
        requires_known_pattern=True,
        critical_context_ok=True,
        action=DecisionAction.AUTO_REMEDIATE_GATED,
        llm=False,
        summary="High confidence + known remediation → gated auto-remediation",
    ),
    PolicyRow(
        band=ConfidenceBand.HIGH,
        requires_known_pattern=False,
        critical_context_ok=True,
        action=DecisionAction.RCA_SUGGEST,
        llm=True,
        summary="High confidence but unknown pattern → limited RCA for suggestions",
    ),
    PolicyRow(
        band=ConfidenceBand.MEDIUM,
        requires_known_pattern=None,
        critical_context_ok=True,
        action=DecisionAction.RCA_SUGGEST,
        llm=True,
        summary="Medium confidence → Bedrock RCA + on-call suggestions",
    ),
    PolicyRow(
        band=ConfidenceBand.LOW,
        requires_known_pattern=None,
        critical_context_ok=True,
        action=DecisionAction.ESCALATE_ONCALL,
        llm=False,
        summary="Low confidence → escalate immediately with explanation",
    ),
    PolicyRow(
        band=ConfidenceBand.MEDIUM,  # band irrelevant when context fails
        requires_known_pattern=None,
        critical_context_ok=False,
        action=DecisionAction.ESCALATE_ONCALL,
        llm=False,
        summary="Missing critical context → escalate (do not trust automation)",
    ),
]


def confidence_band(confidence: float) -> ConfidenceBand:
    if confidence >= settings.confidence_high:
        return ConfidenceBand.HIGH
    if confidence >= settings.confidence_medium:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def missing_critical(missing_context: list[str]) -> list[str]:
    """Intersect detector missing_context with configured critical set."""
    crit = settings.critical_missing_set
    return [m for m in missing_context if m in crit]


def select_action(
    *,
    confidence: float,
    missing_context: list[str],
    has_known_pattern: bool,
    iteration: int,
    max_iterations: int,
) -> tuple[DecisionAction, ConfidenceBand, str]:
    """
    Pure policy function — no I/O.

    Returns (action, band, human_reason) for logging / explainability.
    """
    band = confidence_band(confidence)
    critical = missing_critical(missing_context)

    # Hard stop: iteration budget
    if iteration > max_iterations:
        return (
            DecisionAction.HANDOFF_EXHAUSTED,
            band,
            (
                f"Iteration budget exhausted (iteration={iteration} > "
                f"max={max_iterations}) → forced on-call handoff"
            ),
        )

    # Critical context missing → always escalate (never auto / never LLM spend)
    if critical:
        return (
            DecisionAction.ESCALATE_ONCALL,
            band,
            (
                f"Missing critical context {critical} "
                f"(confidence={confidence:.1f}, band={band.value}) → escalate"
            ),
        )

    if band == ConfidenceBand.HIGH:
        if has_known_pattern:
            return (
                DecisionAction.AUTO_REMEDIATE_GATED,
                band,
                (
                    f"confidence={confidence:.1f} ≥ {settings.confidence_high} "
                    f"AND known remediation pattern → gated auto-remediation "
                    f"(log + propose, no force-execute)"
                ),
            )
        return (
            DecisionAction.RCA_SUGGEST if settings.enable_llm else DecisionAction.ESCALATE_ONCALL,
            band,
            (
                f"confidence={confidence:.1f} ≥ {settings.confidence_high} "
                f"but no known pattern → "
                + (
                    "RCA/LLM for suggestions"
                    if settings.enable_llm
                    else "escalate (LLM disabled)"
                )
            ),
        )

    if band == ConfidenceBand.MEDIUM:
        if not settings.enable_llm:
            return (
                DecisionAction.ESCALATE_ONCALL,
                band,
                (
                    f"confidence={confidence:.1f} in medium band but LLM disabled "
                    f"→ escalate"
                ),
            )
        return (
            DecisionAction.RCA_SUGGEST,
            band,
            (
                f"confidence={confidence:.1f} in "
                f"[{settings.confidence_medium}, {settings.confidence_high}) "
                f"→ call Bedrock RCA (limited) + on-call suggestions"
            ),
        )

    # LOW
    return (
        DecisionAction.ESCALATE_ONCALL,
        band,
        (
            f"confidence={confidence:.1f} < {settings.confidence_medium} "
            f"→ escalate immediately (too uncertain for automation/LLM)"
        ),
    )


def table_as_markdown() -> str:
    """Render DECISION_TABLE for /decision-table and README snippets."""
    lines = [
        "| Band | Known pattern? | Critical context OK? | Action | LLM? | Summary |",
        "|------|----------------|----------------------|--------|------|---------|",
    ]
    for row in DECISION_TABLE:
        kp = (
            "—"
            if row.requires_known_pattern is None
            else ("yes" if row.requires_known_pattern else "no")
        )
        lines.append(
            f"| {row.band.value} | {kp} | "
            f"{'yes' if row.critical_context_ok else 'no'} | "
            f"`{row.action.value}` | {'yes' if row.llm else 'no'} | "
            f"{row.summary} |"
        )
    lines.append("")
    lines.append(
        f"Thresholds: HIGH≥{settings.confidence_high}, "
        f"MEDIUM≥{settings.confidence_medium}, "
        f"max_iterations={settings.max_iterations}."
    )
    return "\n".join(lines)
