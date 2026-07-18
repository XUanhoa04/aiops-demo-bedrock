"""12-factor settings for the feedback collector."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "aiops-feedback-collector"
    port: int = 8005
    streamlit_port: int = 8502
    log_level: str = "INFO"

    incident_manager_url: str = "http://aiops-incident-manager:8002"
    remediation_url: str = "http://aiops-remediation:8004"
    anomaly_detector_url: str = "http://aiops-anomaly-detector:8001"

    feedback_db_path: str = "/data/feedback.db"

    # When true, also PATCH incident-manager.human_feedback + status
    sync_incident_manager: bool = True

    # Threshold tuning defaults (used by /tuning/suggestions + script)
    current_zscore_threshold: float = 2.5
    current_error_rate_threshold: float = 0.15
    fp_rate_warn: float = 0.25  # suggest raise threshold when FP rate ≥ this
    min_samples_for_tuning: int = 5

    default_reviewer: str = "oncall"


settings = Settings()
