from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "aiops-incident-manager"
    port: int = 8002

    redis_url: str = "redis://redis:6379/0"
    redis_queue_anomalies: str = "aiops:anomalies"
    redis_queue_incidents: str = "aiops:incidents"

    # Volume-mounted path in compose; file SQLite is demo-grade only.
    # Production: Postgres/Aurora + migrations (Alembic).
    incident_db_path: str = "/data/incidents.db"

    # Optional Bedrock metadata surfaced on /stats (RCA owns the client)
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    bedrock_model_id: str = "amazon.nova-lite-v1:0"

    # Correlation window: same service + metric within N minutes → one incident
    correlation_window_minutes: int = 10

    # RCA Engine URL (manual analyze + legacy fan-out). Empty = disabled.
    # Env: RCA_ENGINE_URL  e.g. http://aiops-rca-engine:8003
    rca_engine_url: str = "http://aiops-rca-engine:8003"
    rca_timeout_sec: float = 8.0

    # Single control plane (production-like cost control)
    # ----------------------------------------------------
    # Default: Decision Engine owns RCA / remediate / escalate routing.
    # Direct IM → RCA HTTP fan-out is OFF so medium-band-only LLM policy is real.
    # Set ENABLE_DIRECT_RCA_FANOUT=true only for legacy demos that skip DE.
    # Env: ENABLE_DIRECT_RCA_FANOUT / RCA_ALWAYS_ON
    enable_direct_rca_fanout: bool = False

    # Decision Engine — policy routing after ticket create (primary control plane)
    # Env: DECISION_ENGINE_URL  empty = disabled
    decision_engine_url: str = "http://aiops-decision-engine:8006"
    decision_timeout_sec: float = 15.0
    enable_decision_engine: bool = True

    # Enqueue incident JSON on Redis (optional async consumers).
    # RCA redis poll should stay OFF by default so this is not a dual path.
    enable_redis_incident_fanout: bool = True

    # Browser-facing Grafana (for one-click Trace / Logs deep-links in the UI).
    # Must be localhost (or your host DNS), NOT the Docker-internal `lgtm` hostname.
    grafana_public_url: str = "http://localhost:3000"
    tempo_datasource_uid: str = "tempo"
    loki_datasource_uid: str = "loki"
    prometheus_datasource_uid: str = "prometheus"


settings = Settings()
