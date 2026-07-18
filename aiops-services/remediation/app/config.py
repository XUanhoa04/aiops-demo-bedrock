"""12-factor settings for the remediation service."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "aiops-remediation"
    port: int = 8004
    streamlit_port: int = 8501
    log_level: str = "INFO"

    incident_manager_url: str = "http://aiops-incident-manager:8002"
    rca_engine_url: str = "http://aiops-rca-engine:8003"

    checkout_url: str = "http://checkout-service:8080"
    payment_url: str = "http://payment-service:8081"

    # SQLite action history (volume-mounted in compose)
    remediation_db_path: str = "/data/remediation.db"

    # When true, low-risk proposed actions are executed immediately
    auto_execute_low_risk: bool = True

    # Dry-run: never touch docker/chaos; only log simulated results
    simulate_only: bool = False

    # Optional Docker engine for restart/scale demos
    # Empty = no docker client (commands are logged / simulated)
    docker_host: str = ""  # e.g. unix:///var/run/docker.sock
    # Map logical service → docker compose container name
    container_map_json: str = (
        '{"checkout-service":"aiops-checkout","payment-service":"aiops-payment"}'
    )

    # Default actor identity recorded in history
    default_executor: str = "remediation-bot"

    # Operator auth for high-impact mutations (approve / execute / reject / FP).
    # Empty = open localhost demo (logged on /health as auth_required=false).
    # Set REMEDIATION_API_KEY in .env for a production-like gate; Streamlit and
    # curl must send header X-API-Key: <value>.
    remediation_api_key: str = ""


settings = Settings()
