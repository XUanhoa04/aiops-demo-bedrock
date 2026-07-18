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

    # Bedrock reserved for Day-2 enrichment hooks
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-3-5-sonnet-20240620-v1:0"

    # Correlation window: same service + metric within N minutes → one incident
    correlation_window_minutes: int = 10


settings = Settings()
