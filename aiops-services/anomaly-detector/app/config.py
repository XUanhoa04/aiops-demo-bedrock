"""
12-factor configuration for the hybrid anomaly detector + confidence scorer.

Production notes
----------------
* **Pull (query Prometheus) vs remote_write**: for a CV/demo we *query*
  Prometheus on an interval. That keeps the detector stateless-ish, needs no
  write-path privileges on the TSDB, and works with stock grafana/otel-lgtm.
  In production high-cardinality fleets you often prefer a stream processor
  (Kafka + Flink / remote_write receiver) so detection is push-driven and
  near-real-time without scraping load on Prom.
* Thresholds & confidence weights are env-tunable so SRE can hot-tune without
  rebuilds (compose restart is enough).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    # --- service ---
    service_name: str = "aiops-anomaly-detector"
    port: int = 8001
    log_level: str = "INFO"

    # --- Prometheus (LGTM) ---
    # Env: PROMETHEUS_URL
    prometheus_url: str = "http://lgtm:9090"
    # Env: DETECTION_INTERVAL (seconds)
    detection_interval: int = 30

    # Services whose RED signals we watch (comma-separated)
    # Env: WATCHED_SERVICES
    watched_services: str = "checkout-service,payment-service"

    # --- Statistical detector (EWMA + Z-score) ---
    # Env: ZSCORE_THRESHOLD
    zscore_threshold: float = 2.5
    # Env: EWMA_ALPHA
    ewma_alpha: float = 0.3
    # Env: WINDOW_SIZE
    window_size: int = 30
    # Env: MIN_SAMPLES
    min_samples: int = 8

    # Absolute safety nets (cold start)
    # Env: ERROR_RATE_THRESHOLD
    error_rate_threshold: float = 0.15
    # Env: LATENCY_P95_SECONDS_THRESHOLD
    latency_p95_seconds_threshold: float = 0.8

    # --- STL (seasonal) ---
    # Env: ENABLE_STL
    enable_stl: bool = True
    # Env: STL_PERIOD — samples per seasonal cycle (10 × 30s ≈ 5 min micro-cycle)
    stl_period: int = 10
    # Env: STL_MIN_SEASONAL_STRENGTH — var(seasonal)/var(total) gate
    stl_min_seasonal_strength: float = 0.15

    # --- ML detector (IsolationForest) ---
    # Env: IFOREST_CONTAMINATION
    iforest_contamination: float = 0.08
    # Env: IFOREST_N_ESTIMATORS
    iforest_n_estimators: int = 100
    # Env: HYBRID_VOTE = any | majority | all
    hybrid_vote: str = "any"

    # --- Multi-signal context (Loki / Tempo) ---
    # Env: LOKI_URL / TEMPO_URL
    loki_url: str = "http://lgtm:3100"
    tempo_url: str = "http://lgtm:3200"
    # Env: CONTEXT_WINDOW_MINUTES
    context_window_minutes: int = 10
    # Env: CONTEXT_MAX_LOG_LINES / CONTEXT_MAX_TRACES
    context_max_log_lines: int = 30
    context_max_traces: int = 10
    # Env: ENABLE_CONTEXT_GATHER — set false to skip Loki/Tempo (unit tests)
    enable_context_gather: bool = True

    # --- Confidence weights (must be positive; normalized at runtime) ---
    # Rationale: see confidence_scorer.py module docstring.
    # Env: CONFIDENCE_WEIGHT_METRICS / _TRACES / _LOGS / _EVENTS
    confidence_weight_metrics: float = 0.40
    confidence_weight_traces: float = 0.30
    confidence_weight_logs: float = 0.20
    confidence_weight_events: float = 0.10

    # Penalties (points subtracted from 0–100 score)
    # Env: PENALTY_MISSING_METRICS etc.
    penalty_missing_metrics: float = 25.0
    penalty_missing_traces: float = 15.0
    penalty_missing_logs: float = 10.0
    penalty_missing_events: float = 5.0
    penalty_source_down: float = 8.0

    # Minimum confidence to publish to Decision Engine / incident path
    # Env: MIN_CONFIDENCE_TO_NOTIFY (0 = always notify when is_anomaly)
    min_confidence_to_notify: float = 0.0

    # --- Alerting / notification ---
    # Env: REDIS_URL
    redis_url: str = "redis://redis:6379/0"
    # Env: REDIS_QUEUE_ANOMALIES
    redis_queue_anomalies: str = "aiops:anomalies"
    # Dual-publish for Decision Engine (separate queue — no race with IM)
    # Env: REDIS_QUEUE_DECISIONS / ENABLE_DECISION_QUEUE
    redis_queue_decisions: str = "aiops:decisions"
    enable_decision_queue: bool = True
    # Env: INCIDENT_WEBHOOK_URL (empty to disable)
    incident_webhook_url: str = (
        "http://aiops-incident-manager:8002/incidents/from-anomaly"
    )
    # Env: ENABLE_REDIS_NOTIFY
    enable_redis_notify: bool = True
    # Env: ENABLE_WEBHOOK_NOTIFY
    enable_webhook_notify: bool = True
    # Env: ALERT_COOLDOWN_SEC
    alert_cooldown_sec: int = 60

    metrics_path: str = "/metrics"

    @property
    def poll_interval_sec(self) -> int:
        return int(self.detection_interval)

    def watched_service_list(self) -> list[str]:
        return [s.strip() for s in self.watched_services.split(",") if s.strip()]


settings = Settings()
