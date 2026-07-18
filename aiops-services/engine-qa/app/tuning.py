"""
Suggest adjustments to confidence weights and detector/decision thresholds.

Important
---------
Suggestions are **advisory only**. Auto-applying threshold changes from a
feedback loop can hide real outages. Always: review → edit .env → restart
the affected service → re-measure Engine QA metrics.
"""

from __future__ import annotations

from typing import Iterable, Optional

from app.config import settings
from app.models import (
    EngineQualityMetrics,
    QAReview,
    TuningAdvice,
    WeightSuggestion,
)
from app.analytics import compute_quality


def _normalize(w: WeightSuggestion) -> WeightSuggestion:
    s = w.metrics + w.traces + w.logs + w.events
    if s <= 0:
        return WeightSuggestion(metrics=0.4, traces=0.3, logs=0.2, events=0.1)
    return WeightSuggestion(
        metrics=round(w.metrics / s, 4),
        traces=round(w.traces / s, 4),
        logs=round(w.logs / s, 4),
        events=round(w.events / s, 4),
    )


def current_weights() -> WeightSuggestion:
    return _normalize(
        WeightSuggestion(
            metrics=settings.current_confidence_weight_metrics,
            traces=settings.current_confidence_weight_traces,
            logs=settings.current_confidence_weight_logs,
            events=settings.current_confidence_weight_events,
        )
    )


def suggest_tuning(
    reviews: Iterable[QAReview],
    quality: Optional[EngineQualityMetrics] = None,
) -> TuningAdvice:
    rows = list(reviews)
    q = quality or compute_quality(rows)
    cur_w = current_weights()
    details: list[str] = []
    n = q.total_reviews

    advice = TuningAdvice(
        sample_size=n,
        false_positive_rate=q.false_positive_rate,
        hallucination_rate=q.hallucination_rate,
        decision_error_rate=(
            round(1.0 - q.decision_correct_rate, 4) if q.decision_votes else 0.0
        ),
        confidence_reasonable_rate=q.confidence_reasonable_rate,
        recommendation="",
        current_zscore_threshold=settings.current_zscore_threshold,
        current_error_rate_threshold=settings.current_error_rate_threshold,
        current_confidence_weights=cur_w,
        current_confidence_high=settings.current_confidence_high,
        current_confidence_medium=settings.current_confidence_medium,
    )

    if n < settings.min_samples_for_tuning:
        advice.recommendation = (
            f"Insufficient samples ({n} < {settings.min_samples_for_tuning}). "
            "Collect more on-call Engine QA reviews before changing knobs."
        )
        advice.details = [
            "Do not raise thresholds on thin data — you may hide real incidents.",
            f"Current FP rate={q.false_positive_rate:.1%}, "
            f"hallucination={q.hallucination_rate:.1%}.",
        ]
        return advice

    suggested_w = WeightSuggestion(
        metrics=cur_w.metrics,
        traces=cur_w.traces,
        logs=cur_w.logs,
        events=cur_w.events,
    )
    z = settings.current_zscore_threshold
    err_t = settings.current_error_rate_threshold
    high = settings.current_confidence_high
    medium = settings.current_confidence_medium
    actions: list[str] = []

    # --- 1) False positives high → raise detector thresholds ---
    if q.anomaly_votes and q.false_positive_rate >= settings.fp_rate_warn:
        new_z = round(min(5.0, z + 0.5), 2)
        new_err = round(min(0.5, err_t + 0.05), 3)
        advice.suggested_zscore_threshold = new_z
        advice.suggested_error_rate_threshold = new_err
        details.append(
            f"FP rate {q.false_positive_rate:.1%} ≥ {settings.fp_rate_warn:.0%} → "
            f"raise ZSCORE_THRESHOLD {z}→{new_z}, ERROR_RATE_THRESHOLD {err_t}→{new_err}."
        )
        actions.append("raise_detector_thresholds")
        # Also nudge decision medium band up so medium RCA is less eager
        advice.suggested_confidence_medium = round(min(75.0, medium + 5), 1)
        details.append(
            f"Optionally raise CONFIDENCE_MEDIUM {medium}→"
            f"{advice.suggested_confidence_medium} to escalate more, auto less."
        )

    # --- 2) Overconfidence + FP → down-weight metrics or raise high band ---
    if q.overconfidence_count >= max(2, n // 5):
        suggested_w.metrics = max(0.25, suggested_w.metrics - 0.05)
        suggested_w.traces = min(0.40, suggested_w.traces + 0.03)
        suggested_w.logs = min(0.30, suggested_w.logs + 0.02)
        details.append(
            f"Overconfidence: {q.overconfidence_count} FPs with engine_confidence≥70. "
            "Reduce metrics weight; boost traces/logs so multi-signal context gates harder."
        )
        advice.suggested_confidence_high = round(min(95.0, high + 5), 1)
        details.append(
            f"Raise CONFIDENCE_HIGH {high}→{advice.suggested_confidence_high} "
            "so auto-remediate requires stronger corroboration."
        )
        actions.append("rebalance_confidence_weights")

    # --- 3) Confidence not reasonable ---
    if (
        q.confidence_votes >= settings.min_samples_for_tuning
        and q.confidence_reasonable_rate < 0.6
    ):
        if q.mean_confidence_error >= 15:
            # Engine consistently off vs human expected
            if q.mean_engine_confidence > q.mean_expected_confidence + 10:
                suggested_w.events = max(0.05, suggested_w.events - 0.02)
                suggested_w.traces = min(0.40, suggested_w.traces + 0.02)
                details.append(
                    f"Mean conf error={q.mean_confidence_error:.1f}: engine overshoots "
                    f"human expected ({q.mean_engine_confidence:.0f} vs "
                    f"{q.mean_expected_confidence:.0f}). Increase traces weight."
                )
            else:
                suggested_w.metrics = min(0.50, suggested_w.metrics + 0.03)
                details.append(
                    "Engine under-confident vs humans — slight metrics weight increase."
                )
            actions.append("calibrate_confidence")

    # --- 4) LLM hallucination ---
    if (
        q.rca_votes >= 3
        and q.hallucination_rate >= settings.hallucination_rate_warn
    ):
        details.append(
            f"Hallucination rate {q.hallucination_rate:.1%} ≥ "
            f"{settings.hallucination_rate_warn:.0%}. Prefer FORCE_RULE_BASED for "
            "low-evidence incidents; raise MIN_LLM_CONFIDENCE; tighten RCA evidence window."
        )
        advice.suggested_confidence_medium = round(
            max(
                advice.suggested_confidence_medium or medium,
                min(80.0, medium + 5),
            ),
            1,
        )
        details.append(
            "Raise medium band so more cases escalate instead of weak LLM RCA."
        )
        actions.append("reduce_llm_exposure")

    # --- 5) Decision errors ---
    if (
        q.decision_votes >= settings.min_samples_for_tuning
        and (1.0 - q.decision_correct_rate) >= settings.decision_error_rate_warn
    ):
        details.append(
            f"Decision error rate {1 - q.decision_correct_rate:.1%} high. "
            "Review known remediation patterns; consider lower CONFIDENCE_HIGH "
            "if auto path is under-used, or higher if false autos."
        )
        if q.mean_decision_iterations >= 2.5:
            details.append(
                f"Mean iterations before terminal action = {q.mean_decision_iterations:.2f} "
                "(near max). Enrich context earlier or reduce MAX_ITERATIONS churn."
            )
        actions.append("retune_decision_bands")

    # --- 6) Healthy system ---
    if not actions:
        if q.false_positive_rate <= 0.05 and q.precision_estimate >= 0.9:
            new_z = round(max(1.5, z - 0.25), 2)
            advice.suggested_zscore_threshold = new_z
            details.append(
                f"Healthy precision={q.precision_estimate:.0%}, FP={q.false_positive_rate:.1%}. "
                f"Optional: lower ZSCORE_THRESHOLD {z}→{new_z} for earlier detection."
            )
            advice.recommendation = (
                "Engine quality looks good. Optional mild sensitivity increase; "
                "keep monitoring Engine QA weekly."
            )
        else:
            advice.recommendation = (
                "No hard threshold alarms. Keep collecting reviews; "
                f"composite health={q.overall_engine_health:.0%}."
            )
        advice.details = details or ["No changes required at current sample size."]
        advice.env_snippet = _env_snippet(advice, cur_w)
        return advice

    suggested_w = _normalize(suggested_w)
    if suggested_w != cur_w:
        advice.suggested_confidence_weights = suggested_w
        details.append(
            "Suggested CONFIDENCE_WEIGHT_* = "
            f"m={suggested_w.metrics} t={suggested_w.traces} "
            f"l={suggested_w.logs} e={suggested_w.events}"
        )

    advice.recommendation = (
        "Adjust knobs based on observed failure modes: " + ", ".join(actions) + ". "
        "Apply via .env and restart anomaly-detector / decision-engine — do not auto-apply."
    )
    advice.details = details
    advice.env_snippet = _env_snippet(advice, suggested_w)
    return advice


def _env_snippet(advice: TuningAdvice, weights: WeightSuggestion) -> str:
    lines = ["# Suggested .env deltas from Engine QA (review before apply)"]
    if advice.suggested_zscore_threshold is not None:
        lines.append(f"ZSCORE_THRESHOLD={advice.suggested_zscore_threshold}")
    if advice.suggested_error_rate_threshold is not None:
        lines.append(f"ERROR_RATE_THRESHOLD={advice.suggested_error_rate_threshold}")
    if advice.suggested_confidence_weights:
        w = advice.suggested_confidence_weights
        lines.append(f"CONFIDENCE_WEIGHT_METRICS={w.metrics}")
        lines.append(f"CONFIDENCE_WEIGHT_TRACES={w.traces}")
        lines.append(f"CONFIDENCE_WEIGHT_LOGS={w.logs}")
        lines.append(f"CONFIDENCE_WEIGHT_EVENTS={w.events}")
    elif weights:
        lines.append(
            f"# current weights m={weights.metrics} t={weights.traces} "
            f"l={weights.logs} e={weights.events}"
        )
    if advice.suggested_confidence_high is not None:
        lines.append(f"CONFIDENCE_HIGH={advice.suggested_confidence_high}")
    if advice.suggested_confidence_medium is not None:
        lines.append(f"CONFIDENCE_MEDIUM={advice.suggested_confidence_medium}")
    lines.append("# Then: docker compose up -d aiops-anomaly-detector aiops-decision-engine")
    return "\n".join(lines)


def format_tuning_report(advice: TuningAdvice, quality: EngineQualityMetrics) -> str:
    lines = [
        "=== AIOps Engine QA — Tuning Report ===",
        f"samples={advice.sample_size}  composite_health={quality.overall_engine_health:.1%}",
        f"precision≈{quality.precision_estimate:.1%}  recall≈{quality.recall_estimate:.1%}  "
        f"FP={quality.false_positive_rate:.1%}",
        f"confidence_ok={quality.confidence_reasonable_rate:.1%}  "
        f"rca_useful={quality.rca_useful_rate:.1%}  "
        f"hallucination={quality.hallucination_rate:.1%}",
        f"decision_ok={quality.decision_correct_rate:.1%}  "
        f"mean_iterations={quality.mean_decision_iterations:.2f}",
        "",
        f"RECOMMENDATION: {advice.recommendation}",
        "",
        "Details:",
    ]
    for d in advice.details:
        lines.append(f"  • {d}")
    lines.append("")
    lines.append(advice.env_snippet)
    return "\n".join(lines)
