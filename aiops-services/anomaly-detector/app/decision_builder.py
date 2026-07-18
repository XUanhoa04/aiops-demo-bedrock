"""
Assemble DetectionDecision objects for the Decision Engine.

Pipeline
--------
  HybridResult (algorithmic)
       + SignalBundle (multi-signal context)
       + ConfidenceResult
       → DetectionDecision

This module is the single place that maps internal dataclasses → the public
schema in app.models so notifier / API / Prometheus stay consistent.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiops_shared.models import AnomalyEvent, AnomalySeverity

from app.confidence_scorer import ConfidenceScorer, compute_context_completeness
from app.config import settings
from app.context_gatherer import ContextGatherer
from app.detector import HybridResult
from app.models import DetectionDecision, MethodDetail, SignalBundle
from app.prom_metrics import (
    DECISIONS_BUILT,
    set_confidence,
    set_context_completeness,
)

logger = logging.getLogger(__name__)


def _severity_from_score(
    score: float, methods: list[str], confidence: float
) -> AnomalySeverity:
    """
    Severity blends algorithmic magnitude with confidence.

    A high z-score with confidence 20 is still medium — Decision Engine should
    not page on uncorroborated spikes. Confidence ≥ 70 amplifies severity.
    """
    if "manual" in methods or score >= 5.0:
        base = AnomalySeverity.CRITICAL if score >= 8 else AnomalySeverity.HIGH
    elif score >= settings.zscore_threshold * 1.5:
        base = AnomalySeverity.HIGH
    elif score >= settings.zscore_threshold:
        base = AnomalySeverity.MEDIUM
    else:
        base = AnomalySeverity.LOW

    if confidence < 40 and base in (AnomalySeverity.HIGH, AnomalySeverity.CRITICAL):
        return AnomalySeverity.MEDIUM
    if confidence >= 70 and base == AnomalySeverity.MEDIUM:
        return AnomalySeverity.HIGH
    return base


class DecisionBuilder:
    """Owns context gatherer + confidence scorer for the detect path."""

    def __init__(self) -> None:
        self.gatherer = ContextGatherer()
        self.scorer = ConfidenceScorer()

    def close(self) -> None:
        self.gatherer.close()

    def build(
        self,
        result: HybridResult,
        *,
        gather_context: Optional[bool] = None,
    ) -> DetectionDecision:
        do_gather = (
            settings.enable_context_gather
            if gather_context is None
            else gather_context
        )

        signals: SignalBundle
        if do_gather:
            try:
                signals = self.gatherer.gather(
                    result.service,
                    extra_features=result.features,
                )
            except Exception as exc:
                logger.warning("context gather failed: %s", exc)
                signals = SignalBundle(
                    metrics={"instant": dict(result.features)},
                    gather_errors=[str(exc)],
                    sources_ok={"prometheus": True},
                )
                signals.completeness = compute_context_completeness(signals)
        else:
            signals = SignalBundle(
                metrics={"instant": dict(result.features)},
                sources_ok={"prometheus": True},
            )
            signals.completeness = compute_context_completeness(signals)

        conf = self.scorer.score(
            anomaly_score=result.anomaly_score,
            is_anomaly=result.is_anomaly,
            winning_methods=result.winning_methods,
            signals=signals,
            completeness=signals.completeness,
            primary_metric=result.metric,
        )

        explanation = result.explanation
        method = ",".join(result.winning_methods) or result.primary_method
        severity = _severity_from_score(
            result.anomaly_score, result.winning_methods, conf.confidence_score
        )

        method_details = [
            MethodDetail(
                method=m.method,
                score=m.score,
                is_anomaly=m.is_anomaly,
                explanation=m.explanation,
                detail={k: v for k, v in (m.detail or {}).items() if k != "explanation"},
            )
            for m in result.methods
        ]

        decision = DetectionDecision(
            service_name=result.service,
            metric_name=result.metric,
            metric_value=result.value,
            is_anomaly=result.is_anomaly,
            anomaly_score=result.anomaly_score,
            detection_method=method,
            detection_methods=list(result.winning_methods),
            explanation=explanation,
            severity=severity.value,
            threshold=settings.zscore_threshold,
            method_details=method_details,
            features=dict(result.features),
            context=signals,
            context_completeness=signals.completeness.ratio,
            confidence_score=conf.confidence_score,
            confidence_breakdown=conf.confidence_breakdown.model_dump(),
            missing_context=list(conf.missing_context),
            labels={
                "service": result.service,
                "metric": result.metric,
                "detection_method": method,
            },
        )

        # Prometheus observability for Decision Engine health
        set_confidence(
            result.service,
            result.metric,
            conf.confidence_score,
            {
                "metrics": conf.confidence_breakdown.metrics,
                "traces": conf.confidence_breakdown.traces,
                "logs": conf.confidence_breakdown.logs,
                "events": conf.confidence_breakdown.events,
                "algorithm_strength": conf.confidence_breakdown.algorithm_strength,
                "penalties": conf.confidence_breakdown.penalties,
            },
        )
        set_context_completeness(result.service, signals.completeness.ratio)
        DECISIONS_BUILT.labels(
            service=result.service,
            is_anomaly=str(result.is_anomaly).lower(),
        ).inc()

        return decision

    def to_anomaly_event(self, decision: DetectionDecision) -> AnomalyEvent:
        """
        Map DetectionDecision → shared AnomalyEvent for Redis / webhook
        (backward compatible with incident-manager).
        """
        return AnomalyEvent(
            id=decision.id,
            service_name=decision.service_name,
            metric_name=decision.metric_name,
            metric_value=decision.metric_value,
            threshold=decision.threshold,
            severity=AnomalySeverity(decision.severity),
            detector=f"hybrid:{decision.detection_method}",
            message=decision.explanation,
            labels=dict(decision.labels),
            context={
                # Algorithmic
                "anomaly_score": decision.anomaly_score,
                "detection_method": decision.detection_method,
                "detection_methods": decision.detection_methods,
                "explanation": decision.explanation,
                "winning_methods": decision.detection_methods,
                "features": decision.features,
                "method_details": [m.model_dump() for m in decision.method_details],
                "hybrid_vote": settings.hybrid_vote,
                # Confidence engine (Decision Engine primary fields)
                "confidence_score": decision.confidence_score,
                "confidence_breakdown": decision.confidence_breakdown,
                "missing_context": decision.missing_context,
                "context_completeness": decision.context_completeness,
                # Multi-signal snapshot (trimmed for queue size)
                "signals": _trim_signals(decision.context.model_dump()),
                "primary_trace_id": decision.context.primary_trace_id,
                "explainability": {
                    "summary": decision.explanation,
                    "primary_metric": decision.metric_name,
                    "observed_value": decision.metric_value,
                    "score": decision.anomaly_score,
                    "confidence": decision.confidence_score,
                },
            },
            detected_at=decision.detected_at,
            schema_version=decision.schema_version,
        )


def _trim_signals(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep Redis payload bounded — full lines available via /decisions API."""
    logs = raw.get("logs") or []
    traces = raw.get("traces") or []
    events = raw.get("events") or []
    return {
        "metrics": raw.get("metrics") or {},
        "logs": logs[:10],
        "traces": traces[:8],
        "events": events[:8],
        "primary_trace_id": raw.get("primary_trace_id"),
        "sources_ok": raw.get("sources_ok") or {},
        "gather_errors": raw.get("gather_errors") or [],
        "window_start_iso": raw.get("window_start_iso"),
        "window_end_iso": raw.get("window_end_iso"),
        "completeness": raw.get("completeness") or {},
    }
