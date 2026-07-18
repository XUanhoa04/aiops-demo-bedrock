"""
Prometheus exporter metrics for feedback quality (scraped via GET /metrics).

Key series for Grafana "AIOps Engine Health":
  - feedback_positive_rate          overall 👍 rate across cast votes (0–1)
  - rca_accuracy_estimate           👍 rate on RCA useful votes (0–1)
  - false_positive_count            cumulative FP labels (anomaly_correct=false)
  - feedback_submissions_total      counter of reviews submitted
  - feedback_thumbs_total{aspect,vote}  raw vote counters
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

from app.models import FeedbackStats

SERVICE_INFO = Info("aiops_feedback_collector", "Feedback collector metadata")
SERVICE_INFO.info({"component": "feedback-collector", "version": "0.2.0"})

FEEDBACK_POSITIVE_RATE = Gauge(
    "feedback_positive_rate",
    "Share of thumbs-up across all cast feedback votes (0-1)",
)

RCA_ACCURACY_ESTIMATE = Gauge(
    "rca_accuracy_estimate",
    "Share of thumbs-up on RCA useful votes (0-1)",
)

ANOMALY_PRECISION_ESTIMATE = Gauge(
    "anomaly_precision_estimate",
    "Share of thumbs-up on anomaly_correct votes (0-1)",
)

ACTION_SUCCESS_RATE = Gauge(
    "action_success_rate",
    "Share of thumbs-up on action_effective votes (0-1)",
)

FALSE_POSITIVE_COUNT = Gauge(
    "false_positive_count",
    "Number of feedback rows labeling anomaly as incorrect (false positives)",
)

FEEDBACK_SUBMISSIONS = Counter(
    "feedback_submissions_total",
    "Total feedback submissions",
    ["reviewer"],
)

FEEDBACK_THUMBS = Counter(
    "feedback_thumbs_total",
    "Individual thumbs by aspect and vote",
    ["aspect", "vote"],  # aspect=anomaly|rca|action  vote=up|down
)


def refresh_gauges(stats: FeedbackStats) -> None:
    FEEDBACK_POSITIVE_RATE.set(stats.feedback_positive_rate)
    RCA_ACCURACY_ESTIMATE.set(stats.rca_accuracy_estimate)
    ANOMALY_PRECISION_ESTIMATE.set(stats.anomaly_precision_estimate)
    ACTION_SUCCESS_RATE.set(stats.action_success_rate)
    FALSE_POSITIVE_COUNT.set(stats.false_positive_count)


def record_submission(rec_like: object, stats: FeedbackStats) -> None:
    reviewer = getattr(rec_like, "reviewer", None) or "unknown"
    FEEDBACK_SUBMISSIONS.labels(reviewer=str(reviewer)[:64]).inc()

    for aspect, val in (
        ("anomaly", getattr(rec_like, "anomaly_correct", None)),
        ("rca", getattr(rec_like, "rca_useful", None)),
        ("action", getattr(rec_like, "action_effective", None)),
    ):
        if val is None:
            continue
        FEEDBACK_THUMBS.labels(aspect=aspect, vote="up" if val else "down").inc()

    refresh_gauges(stats)
