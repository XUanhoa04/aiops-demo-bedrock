"""
Decision Engine core — limited-iteration policy loop.

Flow
----
  1. Normalize input (confidence, missing_context, signals)
  2. Match known remediation pattern
  3. select_action() from decision_table (pure)
  4. If MEDIUM and context thin → ENRICH (re-score via anomaly-detector) ≤1×
  5. Execute side effects for final action:
       AUTO_REMEDIATE_GATED → log + remediation propose (no force execute)
       RCA_SUGGEST          → RCA/Bedrock (wait) → suggestions
       ESCALATE / HANDOFF   → PATCH incident severity/status + explain
  6. Always emit explainability trail + Prometheus counters

Never loops forever: max_iterations (default 3) forces HANDOFF_EXHAUSTED.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.clients import ServiceClients
from app.config import settings
from app.decision_table import (
    ConfidenceBand,
    DecisionAction,
    confidence_band,
    select_action,
)
from app.models import DecideRequest, EngineDecision, IterationRecord
from app.patterns import find_known_pattern
from app.prom_metrics import (
    DECISIONS_TOTAL,
    ESCALATIONS_TOTAL,
    ITERATIONS_TOTAL,
    LLM_CALLS_TOTAL,
    REMEDIATION_PROPOSALS_TOTAL,
    record_decision,
)

logger = logging.getLogger(__name__)


class DecisionEngine:
    def __init__(self, clients: Optional[ServiceClients] = None) -> None:
        self.clients = clients or ServiceClients()
        self.recent: list[EngineDecision] = []
        self.decided = 0

    def close(self) -> None:
        self.clients.close()

    def decide(self, req: DecideRequest) -> EngineDecision:
        """
        Run the limited iteration loop and return a fully explained decision.
        """
        decision = EngineDecision(
            service_name=req.service_name,
            metric_name=req.metric_name,
            incident_id=req.incident_id,
            anomaly_id=req.anomaly_id,
            action=DecisionAction.ESCALATE_ONCALL,  # placeholder until policy runs
            band=confidence_band(req.confidence_score),
            confidence_score=req.confidence_score,
            confidence_breakdown=dict(req.confidence_breakdown or {}),
            missing_context=list(req.missing_context or []),
            context_completeness=req.context_completeness,
            side_effects_skipped=req.skip_side_effects,
        )
        decision.decision_trace.append(
            f"START service={req.service_name} metric={req.metric_name} "
            f"confidence={req.confidence_score:.1f} "
            f"completeness={req.context_completeness:.2f} "
            f"missing={req.missing_context} "
            f"breakdown={req.confidence_breakdown}"
        )
        logger.warning(
            "decide.start service=%s metric=%s conf=%.1f missing=%s breakdown=%s",
            req.service_name,
            req.metric_name,
            req.confidence_score,
            req.missing_context,
            req.confidence_breakdown,
        )

        # Resolve incident id early if possible (needed for RCA / remediate)
        if not decision.incident_id and req.anomaly_id and not req.skip_side_effects:
            inc = self.clients.find_incident_by_anomaly(req.anomaly_id)
            if inc:
                decision.incident_id = str(inc.get("id"))
                decision.decision_trace.append(
                    f"Resolved incident_id={decision.incident_id} from anomaly_id"
                )

        confidence = float(req.confidence_score)
        missing = list(req.missing_context or [])
        signals = dict(req.signals or {})
        explanation = req.explanation or ""
        max_iter = max(1, int(settings.max_iterations))
        enriched_once = False

        # Force override (tests / ops)
        if req.force_action:
            decision.action = req.force_action
            decision.band = confidence_band(confidence)
            decision.reason = f"force_action={req.force_action.value}"
            decision.decision_trace.append(decision.reason)
            self._execute_action(decision, req, explanation, signals)
            return self._finalize(decision)

        for iteration in range(1, max_iter + 1):
            ITERATIONS_TOTAL.inc()
            pattern = find_known_pattern(
                service_name=req.service_name,
                metric_name=req.metric_name,
                metric_value=req.metric_value,
                explanation=explanation,
                events=(signals.get("events") or [])
                if isinstance(signals.get("events"), list)
                else [],
            )
            has_pattern = pattern is not None
            if pattern:
                decision.known_pattern_id = pattern.pattern.id
                decision.proposed_actions = [pattern.action_text]
                decision.decision_trace.append(f"iter={iteration} {pattern.reason}")

            action, band, reason = select_action(
                confidence=confidence,
                missing_context=missing,
                has_known_pattern=has_pattern,
                iteration=iteration,
                max_iterations=max_iter,
            )

            # Optional enrich before first LLM on medium band if context incomplete
            if (
                action == DecisionAction.RCA_SUGGEST
                and settings.enable_context_refresh
                and not enriched_once
                and missing
                and iteration < max_iter
            ):
                action = DecisionAction.ENRICH_CONTEXT
                reason = (
                    f"Medium/High path with missing_context={missing} → "
                    f"enrich context before LLM (iteration {iteration})"
                )

            rec = IterationRecord(
                iteration=iteration,
                action=action,
                band=band,
                confidence_score=confidence,
                reason=reason,
            )
            decision.iterations.append(rec)
            decision.iteration_count = iteration
            decision.band = band
            decision.confidence_score = confidence
            decision.missing_context = list(missing)
            decision.decision_trace.append(
                f"iter={iteration} band={band.value} action={action.value} | {reason}"
            )
            logger.warning(
                "decide.iter=%s action=%s band=%s conf=%.1f reason=%s",
                iteration,
                action.value,
                band.value,
                confidence,
                reason,
            )

            if action == DecisionAction.ENRICH_CONTEXT:
                enriched_once = True
                new_conf, new_missing, new_signals, notes = self._enrich(req, signals)
                rec.enrichment = {
                    "new_confidence": new_conf,
                    "new_missing": new_missing,
                }
                rec.notes.extend(notes)
                decision.decision_trace.extend(notes)
                if new_conf is not None:
                    confidence = new_conf
                    decision.confidence_score = confidence
                if new_missing is not None:
                    missing = new_missing
                    decision.missing_context = list(missing)
                if new_signals:
                    signals = new_signals
                continue  # re-evaluate policy with richer context

            if action == DecisionAction.HANDOFF_EXHAUSTED:
                decision.action = DecisionAction.ESCALATE_ONCALL
                decision.escalated = True
                decision.escalate_reason = reason
                decision.reason = reason
                self._execute_escalate(decision, req, forced=True)
                return self._finalize(decision)

            # Terminal actions for this loop
            decision.action = action
            decision.reason = reason
            self._execute_action(decision, req, explanation, signals)
            return self._finalize(decision)

        # Safety: should not reach, but handoff if we exit loop without return
        decision.action = DecisionAction.ESCALATE_ONCALL
        decision.escalated = True
        decision.escalate_reason = "loop exit without terminal action"
        decision.reason = decision.escalate_reason
        decision.decision_trace.append(decision.reason)
        self._execute_escalate(decision, req, forced=True)
        return self._finalize(decision)

    # ------------------------------------------------------------------
    # Side effects
    # ------------------------------------------------------------------

    def _execute_action(
        self,
        decision: EngineDecision,
        req: DecideRequest,
        explanation: str,
        signals: dict[str, Any],
    ) -> None:
        if decision.action == DecisionAction.AUTO_REMEDIATE_GATED:
            self._execute_auto_remediate(decision, req)
        elif decision.action == DecisionAction.RCA_SUGGEST:
            self._execute_rca(decision, req)
        else:
            self._execute_escalate(decision, req, forced=False)

    def _execute_auto_remediate(
        self, decision: EngineDecision, req: DecideRequest
    ) -> None:
        """
        HIGH + known pattern: log clearly and propose only (gated).

        We intentionally do NOT call /execute or set force=true.
        auto_execute_low_risk defaults false so human still approves in UI
        unless AUTO_EXECUTE_GATED_LOW_RISK=true for demos.
        """
        actions = decision.proposed_actions or [
            f"investigate {req.service_name} {req.metric_name}"
        ]
        logger.warning(
            "AUTO_REMEDIATE_GATED service=%s pattern=%s actions=%s "
            "confidence=%.1f (will propose, not force-execute)",
            req.service_name,
            decision.known_pattern_id,
            actions,
            decision.confidence_score,
        )
        decision.decision_trace.append(
            f"Gated remediation: pattern={decision.known_pattern_id} "
            f"actions={actions} auto_execute_low_risk="
            f"{settings.auto_execute_gated_low_risk}"
        )

        if req.skip_side_effects or not settings.enable_auto_remediation:
            decision.decision_trace.append(
                "Side effects skipped (skip_side_effects or auto-remediation disabled)"
            )
            return

        if not decision.incident_id:
            decision.decision_trace.append(
                "No incident_id — cannot propose remediation; escalate soft"
            )
            # Soft escalate note only
            return

        result = self.clients.propose_remediation(
            decision.incident_id,
            actions,
            auto_execute_low_risk=settings.auto_execute_gated_low_risk,
        )
        REMEDIATION_PROPOSALS_TOTAL.inc()
        decision.remediation_result = {
            "proposed": result or [],
            "gated": True,
            "auto_execute_low_risk": settings.auto_execute_gated_low_risk,
        }
        decision.decision_trace.append(
            f"Remediation propose returned n={len(result or [])} records"
        )
        notes = (
            f"[decision-engine] AUTO_REMEDIATE_GATED pattern={decision.known_pattern_id} "
            f"confidence={decision.confidence_score:.1f} actions={actions}. "
            f"Gated: no force-execute."
        )
        self.clients.patch_incident(
            decision.incident_id,
            remediation_notes=notes,
            status="remediating",
        )
        decision.incident_patched = True

    def _execute_rca(self, decision: EngineDecision, req: DecideRequest) -> None:
        """
        MEDIUM (and HIGH-without-pattern): call Bedrock via RCA engine only here.

        Token budget is owned by RCA (BEDROCK_MAX_TOKENS). We require wait=true
        so we can read structured JSON + llm confidence and optionally escalate
        if the model itself is unsure.
        """
        decision.llm_called = True
        LLM_CALLS_TOTAL.inc()
        decision.decision_trace.append(
            f"LLM trigger: band={decision.band.value} confidence={decision.confidence_score:.1f} "
            f"— Bedrock only on medium-ish path; context breakdown="
            f"{decision.confidence_breakdown} missing={decision.missing_context}"
        )
        logger.warning(
            "RCA_SUGGEST (LLM) service=%s conf=%.1f breakdown=%s missing=%s",
            req.service_name,
            decision.confidence_score,
            decision.confidence_breakdown,
            decision.missing_context,
        )

        if req.skip_side_effects or not settings.enable_llm:
            decision.suggestions = [
                "Review Grafana RED panels",
                "Check recent deploys / chaos flags",
                "Correlate Tempo error traces for service",
            ]
            decision.decision_trace.append("LLM skipped — static suggestions only")
            return

        if not decision.incident_id:
            decision.decision_trace.append(
                "No incident_id for RCA — escalate with static suggestions"
            )
            decision.suggestions = [
                f"Inspect {req.service_name} metrics around anomaly",
                f"Primary signal: {req.metric_name}={req.metric_value}",
                req.explanation or "See anomaly explanation",
            ]
            decision.action = DecisionAction.ESCALATE_ONCALL
            decision.escalated = True
            decision.escalate_reason = "RCA requires incident_id"
            return

        rca = self.clients.run_rca(
            decision.incident_id,
            wait=settings.rca_wait,
            force=settings.rca_force,
            persist=True,
        )
        if not rca:
            # Fallback to direct path
            rca = self.clients.analyze_incident_direct(
                decision.incident_id, force=True, persist=True
            )

        decision.rca_result = rca
        result = (rca or {}).get("result") or {}
        llm_conf = result.get("confidence")
        if llm_conf is not None:
            try:
                decision.llm_confidence = float(llm_conf)
            except (TypeError, ValueError):
                decision.llm_confidence = None

        suggestions = list(result.get("suggested_actions") or [])
        if result.get("runbook_suggestion"):
            suggestions.append(str(result["runbook_suggestion"]))
        decision.suggestions = suggestions
        decision.decision_trace.append(
            f"RCA mode={(rca or {}).get('mode')} status={(rca or {}).get('status')} "
            f"llm_confidence={decision.llm_confidence} "
            f"root_cause={(result.get('root_cause') or '')[:120]}"
        )

        # If model is unsure, escalate rather than leave on-call with weak RCA
        if (
            decision.llm_confidence is not None
            and decision.llm_confidence < settings.min_llm_confidence
        ):
            decision.decision_trace.append(
                f"LLM confidence {decision.llm_confidence} < "
                f"{settings.min_llm_confidence} → escalate for human judgment"
            )
            decision.action = DecisionAction.ESCALATE_ONCALL
            decision.escalated = True
            decision.escalate_reason = (
                f"LLM confidence too low ({decision.llm_confidence})"
            )
            self._execute_escalate(decision, req, forced=False)
            return

        # Annotate incident with decision + suggestions for on-call
        if settings.patch_incident_context and decision.incident_id:
            note = (
                f"[decision-engine] RCA_SUGGEST conf={decision.confidence_score:.1f} "
                f"llm_conf={decision.llm_confidence} "
                f"suggestions={decision.suggestions[:5]} | {decision.reason}"
            )
            self.clients.patch_incident(
                decision.incident_id,
                remediation_notes=note,
                root_cause=result.get("root_cause"),
                rca_confidence=decision.llm_confidence,
                status="investigating",
            )
            decision.incident_patched = True

    def _execute_escalate(
        self,
        decision: EngineDecision,
        req: DecideRequest,
        *,
        forced: bool,
    ) -> None:
        decision.escalated = True
        if not decision.escalate_reason:
            decision.escalate_reason = decision.reason
        ESCALATIONS_TOTAL.labels(forced=str(forced).lower()).inc()
        decision.decision_trace.append(
            f"ESCALATE forced={forced} reason={decision.escalate_reason} "
            f"confidence={decision.confidence_score:.1f} "
            f"breakdown={decision.confidence_breakdown} "
            f"missing={decision.missing_context}"
        )
        logger.warning(
            "ESCALATE service=%s forced=%s conf=%.1f reason=%s missing=%s",
            req.service_name,
            forced,
            decision.confidence_score,
            decision.escalate_reason,
            decision.missing_context,
        )

        if req.skip_side_effects:
            return
        if not decision.incident_id:
            decision.decision_trace.append("No incident_id — escalate logged only")
            return

        desc_extra = (
            f"\n\n--- Decision Engine ---\n"
            f"Action: escalate_oncall\n"
            f"Reason: {decision.escalate_reason}\n"
            f"Confidence: {decision.confidence_score:.1f}\n"
            f"Breakdown: {decision.confidence_breakdown}\n"
            f"Missing context: {decision.missing_context}\n"
            f"Trace: {' > '.join(decision.decision_trace[-5:])}\n"
        )
        existing = ""
        inc = self.clients.get_incident(decision.incident_id)
        if inc:
            existing = inc.get("description") or ""
        ok = self.clients.patch_incident(
            decision.incident_id,
            status=settings.escalate_status,
            severity=settings.escalate_severity,
            description=(existing + desc_extra)[:8000],
            remediation_notes=(
                f"[decision-engine] ESCALATED: {decision.escalate_reason}"
            ),
        )
        decision.incident_patched = ok

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def _enrich(
        self, req: DecideRequest, signals: dict[str, Any]
    ) -> tuple[Optional[float], Optional[list[str]], dict[str, Any], list[str]]:
        notes: list[str] = []
        body = {
            "service_name": req.service_name,
            "metric_name": req.metric_name,
            "metric_value": req.metric_value,
            "anomaly_score": req.anomaly_score or req.confidence_score / 20.0,
            "is_anomaly": True,
            "winning_methods": [
                m
                for m in (req.detection_method or "ewma_zscore").split(",")
                if m
            ]
            or ["ewma_zscore"],
            "features": req.features
            or {req.metric_name: req.metric_value},
            "gather_context": True,
        }
        notes.append("Calling anomaly-detector /score to refresh multi-signal context")
        scored = self.clients.refresh_confidence(body)
        if not scored:
            notes.append("Enrich failed — keep prior confidence")
            return None, None, signals, notes

        new_conf = scored.get("confidence_score")
        new_missing = scored.get("missing_context")
        new_signals = scored.get("context") or signals
        if isinstance(new_signals, dict) and "metrics" not in new_signals:
            # DetectionDecision nests SignalBundle under "context"
            pass
        notes.append(
            f"Enrich result confidence={new_conf} missing={new_missing} "
            f"completeness={scored.get('context_completeness')}"
        )
        # confidence_breakdown refresh
        if scored.get("confidence_breakdown"):
            req.confidence_breakdown = scored["confidence_breakdown"]
        if scored.get("context_completeness") is not None:
            req.context_completeness = float(scored["context_completeness"])
        return (
            float(new_conf) if new_conf is not None else None,
            list(new_missing) if isinstance(new_missing, list) else None,
            new_signals if isinstance(new_signals, dict) else signals,
            notes,
        )

    def _finalize(self, decision: EngineDecision) -> EngineDecision:
        self.decided += 1
        DECISIONS_TOTAL.labels(action=decision.action.value, band=decision.band.value).inc()
        record_decision(decision)
        self.recent.insert(0, decision)
        self.recent = self.recent[:100]
        logger.warning(
            "decide.done id=%s action=%s band=%s conf=%.1f escalated=%s "
            "llm=%s iterations=%s reason=%s",
            decision.id,
            decision.action.value,
            decision.band.value,
            decision.confidence_score,
            decision.escalated,
            decision.llm_called,
            decision.iteration_count,
            decision.reason,
        )
        return decision

    def status(self) -> dict[str, Any]:
        return {
            "decided": self.decided,
            "recent": len(self.recent),
            "thresholds": {
                "high": settings.confidence_high,
                "medium": settings.confidence_medium,
                "max_iterations": settings.max_iterations,
            },
            "downstream": self.clients.probe(),
        }
