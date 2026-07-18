"""Service configuration via environment (12-factor)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "aiops-anomaly-detector"
    port: int = 8001

    prometheus_url: str = "http://lgtm:9090"
    redis_url: str = "redis://redis:6379/0"
    redis_queue_anomalies: str = "aiops:anomalies"

    anomaly_poll_interval_sec: int = 15
    anomaly_error_rate_threshold: float = 0.15
    anomaly_latency_p95_ms: float = 800.0

    # Sliding window for simple z-score (in-memory; production → TSDB / Flink)
    zscore_window: int = 20
    zscore_sigma: float = 3.0


settings = Settings()
