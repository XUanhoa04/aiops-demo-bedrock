"""
Aggregate quality metrics for AIOps Engine + LLM.

Labeling reality
----------------
True precision/recall need labeled positives *and* known silent failures.
In ops we almost never label non-alerts, so:

  * **Precision estimate** = TP / (TP+FP) from anomaly_correct votes  ← solid
  * **Recall estimate**    = proxy among high-severity reviewed incidents
                             that on-call still says were real anomalies
  * **Hallucination rate** = explicit llm_hallucinated OR (rca 👎 + correction)
  * **Decision iterations** = mean of decision_iterations on reviews

These are *honest estimates* for dashboards, not academic ground truth.
"""

from __future__ import annotations

from typing import Iterable

from app.config import settings
from app.models import EngineQualityMetrics, QAReview


def compute_quality(reviews: Iterable[QAReview]) -> EngineQualityMetrics:
    rows = list(reviews)
    m = EngineQualityMetrics(total_reviews=len(rows))
    if not rows:
        m.notes.append("No QA reviews yet — submit on-call feedback to populate.")
        return m

    # --- Anomaly / precision / FP ---
    a_votes = [r for r in rows if r.anomaly_correct is not None]
    m.anomaly_votes = len(a_votes)
    m.anomaly_true_positive = sum(1 for r in a_votes if r.anomaly_correct is True)
    m.anomaly_false_positive = sum(1 for r in a_votes if r.anomaly_correct is False)
    if m.anomaly_votes:
        m.precision_estimate = m.anomaly_true_positive / m.anomaly_votes
        m.false_positive_rate = m.anomaly_false_positive / m.anomaly_votes

    # Recall proxy: high/critical severity with anomaly vote
    sev_hi = [
        r
        for r in a_votes
        if (r.severity or "").lower() in {"high", "critical"}
    ]
    if sev_hi:
        m.recall_estimate = sum(1 for r in sev_hi if r.anomaly_correct) / len(sev_hi)
        m.notes.append(
            f"Recall estimate from {len(sev_hi)} high/critical reviewed incidents "
            "(not true recall — silent misses are invisible)."
        )
    elif m.anomaly_votes:
        # Fallback: same as precision when no severity signal
        m.recall_estimate = m.precision_estimate
        m.notes.append(
            "Recall estimate falls back to precision (no high-severity labels)."
        )

    # --- Confidence calibration ---
    c_votes = [r for r in rows if r.confidence_reasonable is not None]
    m.confidence_votes = len(c_votes)
    if c_votes:
        m.confidence_reasonable_rate = (
            sum(1 for r in c_votes if r.confidence_reasonable) / len(c_votes)
        )

    eng_confs = [r.engine_confidence for r in rows if r.engine_confidence is not None]
    if eng_confs:
        m.mean_engine_confidence = sum(eng_confs) / len(eng_confs)

    exp_confs = [
        r.expected_confidence for r in rows if r.expected_confidence is not None
    ]
    if exp_confs:
        m.mean_expected_confidence = sum(exp_confs) / len(exp_confs)

    errors = []
    for r in rows:
        if r.engine_confidence is not None and r.expected_confidence is not None:
            errors.append(abs(r.engine_confidence - r.expected_confidence))
    if errors:
        m.mean_confidence_error = sum(errors) / len(errors)

    # Overconfidence: FP while engine claimed high confidence
    m.overconfidence_count = sum(
        1
        for r in rows
        if r.anomaly_correct is False
        and r.engine_confidence is not None
        and r.engine_confidence >= 70.0
    )

    # --- RCA / LLM ---
    rca_votes = [r for r in rows if r.rca_useful is not None]
    m.rca_votes = len(rca_votes)
    if rca_votes:
        m.rca_useful_rate = sum(1 for r in rca_votes if r.rca_useful) / len(rca_votes)

    hallu = [r for r in rows if r.is_hallucination]
    m.hallucination_count = len(hallu)
    # Denominator: reviews that had any RCA signal
    rca_den = max(
        len(rca_votes),
        sum(1 for r in rows if r.llm_hallucinated is not None),
        1 if hallu else 0,
    )
    if rca_votes or hallu:
        den = max(len(rca_votes), sum(1 for r in rows if r.llm_hallucinated is not None))
        den = den or len(hallu)
        m.hallucination_rate = m.hallucination_count / den if den else 0.0

    llm_cs = [r.llm_confidence for r in rows if r.llm_confidence is not None]
    if llm_cs:
        m.mean_llm_confidence = sum(llm_cs) / len(llm_cs)

    # --- Decision engine ---
    d_votes = [r for r in rows if r.decision_correct is not None]
    m.decision_votes = len(d_votes)
    if d_votes:
        m.decision_correct_rate = (
            sum(1 for r in d_votes if r.decision_correct) / len(d_votes)
        )

    iters = [
        float(r.decision_iterations)
        for r in rows
        if r.decision_iterations is not None
    ]
    if iters:
        m.mean_decision_iterations = sum(iters) / len(iters)

    m.handoff_reviews = sum(
        1
        for r in rows
        if (r.decision_action or "").lower()
        in {"escalate_oncall", "handoff_exhausted", "escalate"}
    )

    # --- Composite health (weights chosen for ops dashboards) ---
    # Precision 35%, confidence cal 15%, RCA 25%, decision 25%
    parts = []
    weights = []
    if m.anomaly_votes:
        parts.append(m.precision_estimate)
        weights.append(0.35)
    if m.confidence_votes:
        parts.append(m.confidence_reasonable_rate)
        weights.append(0.15)
    if m.rca_votes:
        # Penalize hallucinations inside RCA score
        rca_score = max(0.0, m.rca_useful_rate - 0.5 * m.hallucination_rate)
        parts.append(rca_score)
        weights.append(0.25)
    if m.decision_votes:
        parts.append(m.decision_correct_rate)
        weights.append(0.25)
    if parts and weights:
        wsum = sum(weights)
        m.overall_engine_health = sum(p * w for p, w in zip(parts, weights)) / wsum
    else:
        m.overall_engine_health = 0.0
        m.notes.append("Composite health needs at least one category of votes.")

    # Round for API cleanliness
    for field in (
        "precision_estimate",
        "recall_estimate",
        "false_positive_rate",
        "confidence_reasonable_rate",
        "mean_engine_confidence",
        "mean_expected_confidence",
        "mean_confidence_error",
        "rca_useful_rate",
        "hallucination_rate",
        "mean_llm_confidence",
        "decision_correct_rate",
        "mean_decision_iterations",
        "overall_engine_health",
    ):
        setattr(m, field, round(float(getattr(m, field)), 4))

    return m
