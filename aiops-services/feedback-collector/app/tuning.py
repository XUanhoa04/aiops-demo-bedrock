"""
Threshold tuning suggestions from false-positive feedback.

Production note
---------------
This is a *heuristic advisor*, not auto-tuning. Auto-raising thresholds
without SRE review can hide real outages. Prefer: show suggestion →
change env → restart detector → measure FP rate again.
"""

from __future__ import annotations

from collections import Counter

from app.config import settings
from app.db import FeedbackRepository
from app.models import TuningSuggestion


def suggest_threshold_adjustments(
    repo: FeedbackRepository | None = None,
) -> TuningSuggestion:
    repo = repo or FeedbackRepository()
    stats = repo.compute_stats()
    fps = repo.list_false_positives(limit=200)

    anomaly_votes = stats.with_anomaly_vote
    fp_count = stats.false_positive_count
    fp_rate = (fp_count / anomaly_votes) if anomaly_votes else 0.0

    z = settings.current_zscore_threshold
    err_t = settings.current_error_rate_threshold
    details: list[str] = []
    services = [f.service_name or "unknown" for f in fps]
    top_services = [s for s, _ in Counter(services).most_common(5)]

    if anomaly_votes < settings.min_samples_for_tuning:
        return TuningSuggestion(
            false_positive_count=fp_count,
            anomaly_votes=anomaly_votes,
            false_positive_rate=round(fp_rate, 4),
            recommendation=(
                f"Insufficient samples ({anomaly_votes} < "
                f"{settings.min_samples_for_tuning}). Collect more on-call "
                f"reviews before changing thresholds."
            ),
            suggested_zscore_threshold=None,
            suggested_error_rate_threshold=None,
            current_zscore_threshold=z,
            current_error_rate_threshold=err_t,
            details=[
                f"Need at least {settings.min_samples_for_tuning} anomaly votes.",
                f"Current FP count={fp_count}, rate={fp_rate:.1%}.",
            ],
            sample_fp_services=top_services,
        )

    if fp_rate >= settings.fp_rate_warn:
        # Raise thresholds → fewer alerts (less sensitive)
        new_z = round(min(5.0, z + 0.5), 2)
        new_err = round(min(0.5, err_t + 0.05), 3)
        details.append(
            f"False-positive rate {fp_rate:.1%} ≥ warn level "
            f"{settings.fp_rate_warn:.0%}."
        )
        details.append(
            f"Suggest raising ZSCORE_THRESHOLD {z} → {new_z} "
            f"and ERROR_RATE_THRESHOLD {err_t} → {new_err}."
        )
        details.append(
            "Apply via .env then: docker compose up -d aiops-anomaly-detector"
        )
        if top_services:
            details.append(
                "FP hotspots (services): " + ", ".join(top_services)
            )
        for fp in fps[:5]:
            if fp.comment:
                details.append(f"FP note on {fp.incident_id[:8]}: {fp.comment[:120]}")

        return TuningSuggestion(
            false_positive_count=fp_count,
            anomaly_votes=anomaly_votes,
            false_positive_rate=round(fp_rate, 4),
            recommendation=(
                "False positives are high — increase detector thresholds "
                "to reduce noise (trade: may miss weak anomalies)."
            ),
            suggested_zscore_threshold=new_z,
            suggested_error_rate_threshold=new_err,
            current_zscore_threshold=z,
            current_error_rate_threshold=err_t,
            details=details,
            sample_fp_services=top_services,
        )

    if fp_rate <= 0.05 and stats.anomaly_precision_estimate >= 0.9:
        # Very clean — could slightly lower for earlier detection
        new_z = round(max(1.5, z - 0.25), 2)
        details.append(
            f"FP rate low ({fp_rate:.1%}) and anomaly precision high "
            f"({stats.anomaly_precision_estimate:.1%})."
        )
        details.append(
            f"Optional: slightly lower ZSCORE_THRESHOLD {z} → {new_z} "
            "for earlier detection (monitor carefully)."
        )
        return TuningSuggestion(
            false_positive_count=fp_count,
            anomaly_votes=anomaly_votes,
            false_positive_rate=round(fp_rate, 4),
            recommendation=(
                "Precision looks good. Optional mild threshold decrease "
                "for earlier detection; keep an eye on alert volume."
            ),
            suggested_zscore_threshold=new_z,
            suggested_error_rate_threshold=err_t,
            current_zscore_threshold=z,
            current_error_rate_threshold=err_t,
            details=details,
            sample_fp_services=top_services,
        )

    details.append(
        f"FP rate {fp_rate:.1%} is within acceptable band "
        f"(warn at {settings.fp_rate_warn:.0%}). No change required."
    )
    return TuningSuggestion(
        false_positive_count=fp_count,
        anomaly_votes=anomaly_votes,
        false_positive_rate=round(fp_rate, 4),
        recommendation="Thresholds look balanced given current feedback.",
        suggested_zscore_threshold=None,
        suggested_error_rate_threshold=None,
        current_zscore_threshold=z,
        current_error_rate_threshold=err_t,
        details=details,
        sample_fp_services=top_services,
    )


def format_tuning_report(s: TuningSuggestion) -> str:
    lines = [
        "=== AIOps threshold tuning suggestion ===",
        f"Anomaly votes:     {s.anomaly_votes}",
        f"False positives:   {s.false_positive_count}",
        f"FP rate:           {s.false_positive_rate:.1%}",
        f"Current ZSCORE:    {s.current_zscore_threshold}",
        f"Current ERR_RATE:  {s.current_error_rate_threshold}",
        f"Recommendation:    {s.recommendation}",
    ]
    if s.suggested_zscore_threshold is not None:
        lines.append(f"Suggested ZSCORE:  {s.suggested_zscore_threshold}")
    if s.suggested_error_rate_threshold is not None:
        lines.append(f"Suggested ERR_RATE:{s.suggested_error_rate_threshold}")
    if s.sample_fp_services:
        lines.append("FP services:       " + ", ".join(s.sample_fp_services))
    lines.append("--- details ---")
    lines.extend(f"  • {d}" for d in s.details)
    if s.suggested_zscore_threshold is not None:
        lines.extend(
            [
                "",
                "# Example .env patch:",
                f"ZSCORE_THRESHOLD={s.suggested_zscore_threshold}",
                f"ERROR_RATE_THRESHOLD={s.suggested_error_rate_threshold}",
            ]
        )
    return "\n".join(lines)
