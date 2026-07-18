"""
Prometheus *exporter* metrics for this service (scraped on GET /metrics).

We deliberately expose:
  - anomaly_score{service,metric,method}        continuous algorithmic score
  - is_anomaly{service,metric,method}           0/1 gauge
  - detection_method{service,metric,method}     1 for methods that fired
  - anomaly_confidence_score{service,metric}    0–100 Decision Engine trust
  - context_completeness{service}               0–1 multi-signal coverage
  - detector_polls_total / detector_errors_total operational SLIs

Production note: keep label cardinality low (service × metric × method).
Never put high-cardinality IDs (order_id, user_id, trace_id) on these labels.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

SERVICE_INFO = Info("aiops_anomaly_detector", "Anomaly detector build metadata")
SERVICE_INFO.info({"component": "anomaly-detector", "version": "0.3.0"})

# Continuous anomaly score (higher = more anomalous). Method-specific.
ANOMALY_SCORE = Gauge(
    "anomaly_score",
    "Latest anomaly score for a service metric (higher is more anomalous)",
    ["service", "metric", "method"],
)

# Binary flag scraped into Grafana panels / alert rules.
IS_ANOMALY = Gauge(
    "is_anomaly",
    "1 if the last evaluation marked this series anomalous, else 0",
    ["service", "metric", "method"],
)

# Which method "owns" the hybrid decision (set to 1 for the winning method(s)).
DETECTION_METHOD = Gauge(
    "detection_method",
    "1 if this method contributed to the latest hybrid decision",
    ["service", "metric", "method"],
)

# ---------------------------------------------------------------------------
# Confidence + context completeness (Decision Engine observability)
# ---------------------------------------------------------------------------

# Named exactly as requested in the brief so Grafana / alerts can bind easily.
ANOMALY_CONFIDENCE_SCORE = Gauge(
    "anomaly_confidence_score",
    "0–100 confidence that the latest anomaly is real & actionable "
    "(metrics/traces/logs/events weighted; penalties for missing context)",
    ["service", "metric"],
)

CONTEXT_COMPLETENESS = Gauge(
    "context_completeness",
    "0–1 fraction of multi-signal context families present "
    "(metrics, trace_id, logs, events) for the service",
    ["service"],
)

# Breakdown gauges for tuning weight/penalty knobs in Grafana
CONFIDENCE_BREAKDOWN = Gauge(
    "anomaly_confidence_breakdown",
    "Points contributed (or subtracted for penalties) per confidence bucket",
    ["service", "metric", "bucket"],
)

POLLS_TOTAL = Counter(
    "detector_polls_total",
    "Number of detection poll cycles completed",
)

ERRORS_TOTAL = Counter(
    "detector_errors_total",
    "Number of poll / notify errors",
    ["stage"],
)

ANOMALIES_EMITTED = Counter(
    "detector_anomalies_emitted_total",
    "Anomalies published to Redis/webhook",
    ["service", "metric", "method"],
)

DECISIONS_BUILT = Counter(
    "detector_decisions_built_total",
    "DetectionDecision objects assembled for the Decision Engine",
    ["service", "is_anomaly"],
)


def set_score(service: str, metric: str, method: str, score: float, is_anom: bool) -> None:
    ANOMALY_SCORE.labels(service=service, metric=metric, method=method).set(score)
    IS_ANOMALY.labels(service=service, metric=metric, method=method).set(
        1.0 if is_anom else 0.0
    )


def set_detection_methods(
    service: str,
    metric: str,
    methods: dict[str, bool],
) -> None:
    for method, active in methods.items():
        DETECTION_METHOD.labels(service=service, metric=metric, method=method).set(
            1.0 if active else 0.0
        )


def set_confidence(
    service: str,
    metric: str,
    confidence: float,
    breakdown: dict[str, float] | None = None,
) -> None:
    ANOMALY_CONFIDENCE_SCORE.labels(service=service, metric=metric).set(
        max(0.0, min(100.0, confidence))
    )
    if breakdown:
        for bucket, value in breakdown.items():
            if bucket in {"weights", "penalty_reasons"}:
                continue
            try:
                CONFIDENCE_BREAKDOWN.labels(
                    service=service, metric=metric, bucket=str(bucket)
                ).set(float(value))
            except (TypeError, ValueError):
                continue


def set_context_completeness(service: str, ratio: float) -> None:
    CONTEXT_COMPLETENESS.labels(service=service).set(max(0.0, min(1.0, ratio)))
