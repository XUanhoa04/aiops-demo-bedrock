"""
Confidence Scoring Engine — the gate between "interesting metric blip" and
"Decision Engine may auto-ticket / auto-remediate".

Design goals
------------
1. **Multi-signal trust, not single-threshold.** A lone p99 spike with no logs
   or traces is often a scrape glitch or cold-start noise; the same spike with
   error traces + ERROR logs is high confidence.
2. **Explainable arithmetic.** Every point is attributable to a weight bucket
   so on-call can see *why* confidence is 72 vs 40 (interview + tuning UI).
3. **Penalize missing critical context.** Missing traces when latency/error is
   the primary metric is more damaging than missing deploy events.

Default weights (configurable via env)
--------------------------------------
  Metrics : 40%  — first-class detector input; always present if we fired.
  Traces  : 30%  — strongest causal link (which span failed / how slow).
  Logs    : 20%  — message-level corroboration (exceptions, error codes).
  Events  : 10%  — change/deploy/chaos correlation (powerful but sparse).

Why these numbers?
------------------
* Metrics dominate because *this service is metric-driven*: without a solid
  PromQL anomaly we should not be here. 40% keeps algorithm strength relevant
  even when Tempo is empty (common on tiny demos).
* Traces > Logs: a stack trace in logs is useful, but a Tempo error span
  answers "which dependency?" for RCA. In production SRE studies, correlated
  traces cut MTTR more than extra log volume.
* Events are low weight by default because change data is often incomplete
  (no CMDB hook in this demo). When present they *boost* a lot relative to
  their weight via quality scaling; when absent we apply a small penalty only
  if other signals are also thin.

Output
------
  confidence_score   : float 0–100
  confidence_breakdown : per-bucket points + penalties + weights used
  missing_context    : list[str] for Decision Engine gating UI
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.config import settings
from app.models import (
    ConfidenceBreakdown,
    ConfidenceResult,
    ContextCompleteness,
    SignalBundle,
)

logger = logging.getLogger(__name__)


class ConfidenceScorer:
    """
    Pure function-ish scorer: given algorithmic result + signal bundle → score.

    Stateless so it is trivial to unit-test and safe to call from the poll loop.
    """

    def __init__(
        self,
        weight_metrics: Optional[float] = None,
        weight_traces: Optional[float] = None,
        weight_logs: Optional[float] = None,
        weight_events: Optional[float] = None,
    ) -> None:
        # Allow injection for tests; default from 12-factor settings.
        self.w_metrics = (
            weight_metrics
            if weight_metrics is not None
            else settings.confidence_weight_metrics
        )
        self.w_traces = (
            weight_traces
            if weight_traces is not None
            else settings.confidence_weight_traces
        )
        self.w_logs = (
            weight_logs if weight_logs is not None else settings.confidence_weight_logs
        )
        self.w_events = (
            weight_events
            if weight_events is not None
            else settings.confidence_weight_events
        )
        self._normalize_weights()

    def _normalize_weights(self) -> None:
        total = self.w_metrics + self.w_traces + self.w_logs + self.w_events
        if total <= 0:
            # Hard fallback — should never happen with defaults
            self.w_metrics, self.w_traces, self.w_logs, self.w_events = (
                0.40,
                0.30,
                0.20,
                0.10,
            )
            return
        self.w_metrics /= total
        self.w_traces /= total
        self.w_logs /= total
        self.w_events /= total

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        *,
        anomaly_score: float,
        is_anomaly: bool,
        winning_methods: list[str],
        signals: SignalBundle,
        completeness: Optional[ContextCompleteness] = None,
        primary_metric: str = "",
    ) -> ConfidenceResult:
        """
        Compute confidence in [0, 100].

        Algorithm
        ---------
        1. For each signal family, compute a quality ∈ [0, 1].
        2. contribution_i = weight_i * 100 * quality_i
        3. Add a small algorithm_strength bonus from anomaly_score / method votes
           (capped) — rewards multi-method consensus without letting score alone
           hit 100 without context.
        4. Subtract penalties for missing *critical* context.
        5. Clamp to [0, 100].
        """
        completeness = completeness or signals.completeness
        missing = list(completeness.missing)

        q_metrics = self._quality_metrics(signals, anomaly_score, is_anomaly)
        q_traces = self._quality_traces(signals)
        q_logs = self._quality_logs(signals)
        q_events = self._quality_events(signals)

        pts_metrics = self.w_metrics * 100.0 * q_metrics
        pts_traces = self.w_traces * 100.0 * q_traces
        pts_logs = self.w_logs * 100.0 * q_logs
        pts_events = self.w_events * 100.0 * q_events

        # Algorithm strength: multi-method consensus + magnitude of score.
        # Cap at +15 so pure "loud metric, empty context" cannot look certain.
        algo_bonus = self._algorithm_bonus(anomaly_score, winning_methods, is_anomaly)

        raw = pts_metrics + pts_traces + pts_logs + pts_events + algo_bonus
        penalties, penalty_reasons = self._penalties(
            completeness=completeness,
            signals=signals,
            primary_metric=primary_metric,
            q_metrics=q_metrics,
            q_traces=q_traces,
            q_logs=q_logs,
        )
        final = max(0.0, min(100.0, raw - penalties))

        # Ensure missing_context always lists anything still absent
        for item in completeness.missing:
            if item not in missing:
                missing.append(item)

        breakdown = ConfidenceBreakdown(
            metrics=round(pts_metrics, 2),
            traces=round(pts_traces, 2),
            logs=round(pts_logs, 2),
            events=round(pts_events, 2),
            algorithm_strength=round(algo_bonus, 2),
            penalties=round(penalties, 2),
            penalty_reasons=penalty_reasons,
            weights={
                "metrics": round(self.w_metrics, 4),
                "traces": round(self.w_traces, 4),
                "logs": round(self.w_logs, 4),
                "events": round(self.w_events, 4),
            },
        )

        logger.info(
            "confidence=%.1f metrics=%.1f traces=%.1f logs=%.1f events=%.1f "
            "algo=%.1f penalties=%.1f missing=%s",
            final,
            pts_metrics,
            pts_traces,
            pts_logs,
            pts_events,
            algo_bonus,
            penalties,
            missing,
        )

        return ConfidenceResult(
            confidence_score=round(final, 2),
            confidence_breakdown=breakdown,
            missing_context=missing,
        )

    # ------------------------------------------------------------------
    # Quality scorers (0–1)
    # ------------------------------------------------------------------

    def _quality_metrics(
        self,
        signals: SignalBundle,
        anomaly_score: float,
        is_anomaly: bool,
    ) -> float:
        """
        Metrics quality = presence of RED features × how strong the anomaly is.

        We map anomaly_score through a soft curve so z≈2.5 → ~0.55, z≈5 → ~0.9.
        """
        metrics = signals.metrics or {}
        instant = metrics.get("instant") or metrics
        # Count non-null RED features
        keys = (
            "http_error_rate",
            "http_request_rate",
            "http_latency_p95_seconds",
            "http_latency_p99_seconds",
        )
        present = 0
        for k in keys:
            v = instant.get(k) if isinstance(instant, dict) else None
            if v is None and isinstance(metrics, dict):
                v = metrics.get(k)
            if v is not None:
                present += 1
        if present == 0 and not is_anomaly:
            return 0.0
        coverage = max(present / 3.0, 0.35 if is_anomaly else 0.0)

        # Strength from anomaly_score (z-score-ish units)
        # logistic-ish: 1 - exp(-score / 3)
        strength = 1.0 - pow(2.718281828, -max(anomaly_score, 0.0) / 3.0)
        strength = max(0.15 if is_anomaly else 0.0, min(1.0, strength))

        # Source must be healthy when claimed
        if signals.sources_ok.get("prometheus") is False:
            coverage *= 0.4

        return max(0.0, min(1.0, 0.55 * coverage + 0.45 * strength))

    def _quality_traces(self, signals: SignalBundle) -> float:
        if not signals.traces and not signals.primary_trace_id:
            return 0.0
        n = len(signals.traces)
        has_primary = 1.0 if signals.primary_trace_id else 0.0
        # Diminishing returns after a handful of error/slow traces
        volume = min(1.0, n / 5.0)
        # Prefer traces that came from error TraceQL
        errorish = sum(
            1
            for t in signals.traces
            if "error" in str(t.get("search_mode") or "").lower()
            or (t.get("duration_ms") or 0) > 500
        )
        error_ratio = (errorish / n) if n else 0.0
        q = 0.35 * has_primary + 0.40 * volume + 0.25 * error_ratio
        if signals.sources_ok.get("tempo") is False:
            q *= 0.3
        return max(0.0, min(1.0, q))

    def _quality_logs(self, signals: SignalBundle) -> float:
        logs = signals.logs or []
        if not logs:
            return 0.0
        n = len(logs)
        volume = min(1.0, n / 10.0)
        with_tid = sum(1 for row in logs if row.get("trace_id"))
        corr = (with_tid / n) if n else 0.0
        # Keyword density: error-ish lines already filtered by gatherer
        q = 0.55 * volume + 0.45 * corr
        if signals.sources_ok.get("loki") is False:
            q *= 0.3
        return max(0.0, min(1.0, q))

    def _quality_events(self, signals: SignalBundle) -> float:
        events = signals.events or []
        if not events:
            return 0.0
        # Any recent change/chaos/deploy event in the window is high signal
        n = len(events)
        high = sum(
            1
            for e in events
            if str(e.get("severity") or e.get("type") or "").lower()
            in {"high", "critical", "deploy", "chaos", "restart", "change"}
        )
        return max(0.0, min(1.0, 0.5 + 0.5 * min(1.0, (n + high) / 3.0)))

    def _algorithm_bonus(
        self,
        anomaly_score: float,
        winning_methods: list[str],
        is_anomaly: bool,
    ) -> float:
        if not is_anomaly:
            return 0.0
        # Consensus: each additional method beyond the first adds trust
        n = len(winning_methods) or 1
        consensus = min(8.0, (n - 1) * 3.0)  # 0, 3, 6, 8…
        magnitude = min(7.0, max(0.0, anomaly_score) * 1.2)
        return min(15.0, consensus + magnitude * 0.5)

    def _penalties(
        self,
        *,
        completeness: ContextCompleteness,
        signals: SignalBundle,
        primary_metric: str,
        q_metrics: float,
        q_traces: float,
        q_logs: float,
    ) -> tuple[float, list[str]]:
        """
        Subtract points when context the Decision Engine needs is missing.

        Rationale for magnitudes:
          - No metrics at all → huge penalty (should be rare if we fired).
          - No trace_id on latency/error anomalies → moderate (RCA is blind).
          - No related logs → smaller (still useful metric-only tickets).
          - Backend source down → treat as missing (do not invent confidence).
        """
        pen = 0.0
        reasons: list[str] = []

        if not completeness.has_sufficient_metrics or q_metrics < 0.2:
            pen += settings.penalty_missing_metrics
            reasons.append(
                f"insufficient_metrics (-{settings.penalty_missing_metrics})"
            )

        if not completeness.has_trace_id and not signals.primary_trace_id:
            # Heavier when the primary metric is latency or error (traces matter)
            base = settings.penalty_missing_traces
            metric = (primary_metric or "").lower()
            if "latency" in metric or "error" in metric:
                base = min(30.0, base * 1.25)
            pen += base
            reasons.append(f"missing_trace_id (-{base})")

        if not completeness.has_related_logs or q_logs < 0.05:
            pen += settings.penalty_missing_logs
            reasons.append(f"missing_related_logs (-{settings.penalty_missing_logs})")

        # Events are optional — only light nudge when *everything else* is weak
        if (
            not completeness.has_events
            and q_traces < 0.2
            and q_logs < 0.2
            and settings.penalty_missing_events > 0
        ):
            pen += settings.penalty_missing_events
            reasons.append(
                f"missing_change_events (-{settings.penalty_missing_events})"
            )

        # Source outages: stack is lying by omission
        for src, ok in (signals.sources_ok or {}).items():
            if ok is False:
                pen += settings.penalty_source_down
                reasons.append(f"source_down:{src} (-{settings.penalty_source_down})")

        return pen, reasons


def compute_context_completeness(signals: SignalBundle) -> ContextCompleteness:
    """
    Derive the checklist from a gathered SignalBundle.

    Expected families: metrics, logs, traces (trace_id), events.
    ratio = present / 4.
    """
    instant = (signals.metrics or {}).get("instant") or signals.metrics or {}
    metric_vals = []
    if isinstance(instant, dict):
        for k in (
            "http_error_rate",
            "http_request_rate",
            "http_latency_p95_seconds",
            "http_latency_p99_seconds",
        ):
            if instant.get(k) is not None:
                metric_vals.append(instant[k])
    # Also accept raw feature map from detector path
    if not metric_vals and isinstance(signals.metrics, dict):
        for k, v in signals.metrics.items():
            if k in ("instant", "range", "status_code_rates", "service", "window"):
                continue
            if isinstance(v, (int, float)):
                metric_vals.append(v)

    has_metrics = len(metric_vals) >= 1 or bool(
        (signals.metrics or {}).get("range")
    )
    # "Sufficient" = at least 2 RED signals or 1 with range history
    has_sufficient_metrics = len(metric_vals) >= 2 or (
        len(metric_vals) >= 1
        and isinstance((signals.metrics or {}).get("range"), dict)
        and any(
            (signals.metrics.get("range") or {}).get(k, {}).get("points", 0) > 0
            for k in (
                "http_error_rate",
                "http_request_rate",
                "http_latency_p95_seconds",
            )
        )
    )

    has_logs = len(signals.logs or []) > 0
    has_trace_id = bool(signals.primary_trace_id) or any(
        t.get("trace_id") for t in (signals.traces or [])
    ) or any(row.get("trace_id") for row in (signals.logs or []))
    has_events = len(signals.events or []) > 0

    missing: list[str] = []
    if not has_sufficient_metrics:
        missing.append("sufficient_metrics")
    if not has_trace_id:
        missing.append("trace_id")
    if not has_logs:
        missing.append("related_logs")
    if not has_events:
        missing.append("events_or_change")

    flags = [
        has_sufficient_metrics,
        has_trace_id,
        has_logs,
        has_events,
    ]
    ratio = sum(1 for f in flags if f) / 4.0

    return ContextCompleteness(
        has_trace_id=has_trace_id,
        has_related_logs=has_logs,
        has_sufficient_metrics=has_sufficient_metrics or has_metrics,
        has_events=has_events,
        ratio=round(ratio, 4),
        missing=missing,
    )
