"""
Prometheus metrics for Engine QA meta-SLO dashboards.

Series
------
  engine_qa_precision_estimate
  engine_qa_recall_estimate
  engine_qa_false_positive_rate
  engine_qa_hallucination_rate
  engine_qa_decision_correct_rate
  engine_qa_mean_decision_iterations
  engine_qa_overall_health
  engine_qa_reviews_total
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

from app.models import EngineQualityMetrics, QAReview

SERVICE_INFO = Info("aiops_engine_qa", "Engine QA metadata")
SERVICE_INFO.info({"component": "engine-qa", "version": "0.1.0"})

PRECISION = Gauge(
    "engine_qa_precision_estimate",
    "Estimated detector precision from on-call anomaly votes (0-1)",
)
RECALL = Gauge(
    "engine_qa_recall_estimate",
    "Proxy recall among high-severity reviewed incidents (0-1)",
)
FP_RATE = Gauge(
    "engine_qa_false_positive_rate",
    "False positive rate from anomaly_correct=false votes (0-1)",
)
CONF_OK = Gauge(
    "engine_qa_confidence_reasonable_rate",
    "Share of reviews where confidence was judged reasonable (0-1)",
)
HALLUCINATION = Gauge(
    "engine_qa_hallucination_rate",
    "LLM hallucination rate estimate (0-1)",
)
RCA_USEFUL = Gauge(
    "engine_qa_rca_useful_rate",
    "Share of RCA votes marked useful (0-1)",
)
DECISION_OK = Gauge(
    "engine_qa_decision_correct_rate",
    "Share of decision votes marked correct (0-1)",
)
MEAN_ITERS = Gauge(
    "engine_qa_mean_decision_iterations",
    "Mean decision-engine iterations before terminal action",
)
OVERALL = Gauge(
    "engine_qa_overall_health",
    "Blended engine health score (0-1)",
)
REVIEWS_TOTAL = Counter(
    "engine_qa_reviews_total",
    "Total Engine QA reviews submitted",
    ["reviewer"],
)
THUMBS = Counter(
    "engine_qa_thumbs_total",
    "Individual Engine QA thumbs",
    ["aspect", "vote"],
)


def refresh_gauges(q: EngineQualityMetrics) -> None:
    PRECISION.set(q.precision_estimate)
    RECALL.set(q.recall_estimate)
    FP_RATE.set(q.false_positive_rate)
    CONF_OK.set(q.confidence_reasonable_rate)
    HALLUCINATION.set(q.hallucination_rate)
    RCA_USEFUL.set(q.rca_useful_rate)
    DECISION_OK.set(q.decision_correct_rate)
    MEAN_ITERS.set(q.mean_decision_iterations)
    OVERALL.set(q.overall_engine_health)


def record_review(rec: QAReview, q: EngineQualityMetrics) -> None:
    REVIEWS_TOTAL.labels(reviewer=(rec.reviewer or "unknown")[:64]).inc()
    for aspect, val in (
        ("anomaly", rec.anomaly_correct),
        ("confidence", rec.confidence_reasonable),
        ("rca", rec.rca_useful),
        ("decision", rec.decision_correct),
        ("hallucination", rec.llm_hallucinated),
    ):
        if val is None:
            continue
        # hallucination True = bad → vote=down for consistency
        if aspect == "hallucination":
            THUMBS.labels(aspect=aspect, vote="yes" if val else "no").inc()
        else:
            THUMBS.labels(aspect=aspect, vote="up" if val else "down").inc()
    refresh_gauges(q)
