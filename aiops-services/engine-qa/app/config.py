"""
12-factor settings for Engine QA ("watch the watcher").

Why a separate service from feedback-collector?
-----------------------------------------------
feedback-collector = per-incident on-call thumbs (ops UX).
engine-qa          = aggregate quality of the *AIOps engine itself* + LLM:
  precision/recall estimates, FP rate, hallucination rate, decision-loop
  stats, and *suggested* confidence-weight / threshold changes.

Production: this is the meta-SLO layer — without it you cannot tell if
detector/RCA/decision policies are drifting.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str = "aiops-engine-qa"
    port: int = 8007
    streamlit_port: int = 8503
    log_level: str = "INFO"

    # Downstream
    incident_manager_url: str = "http://aiops-incident-manager:8002"
    anomaly_detector_url: str = "http://aiops-anomaly-detector:8001"
    decision_engine_url: str = "http://aiops-decision-engine:8006"
    rca_engine_url: str = "http://aiops-rca-engine:8003"
    feedback_url: str = "http://aiops-feedback-collector:8005"
    remediation_url: str = "http://aiops-remediation:8004"

    qa_db_path: str = "/data/engine_qa.db"
    default_reviewer: str = "oncall-sre"

    # Also push a summary vote into feedback-collector when possible
    sync_feedback_collector: bool = True
    sync_incident_manager: bool = True

    # Current knobs (for tuning suggestions — mirror detector/decision env)
    current_zscore_threshold: float = 2.5
    current_error_rate_threshold: float = 0.15
    current_confidence_weight_metrics: float = 0.40
    current_confidence_weight_traces: float = 0.30
    current_confidence_weight_logs: float = 0.20
    current_confidence_weight_events: float = 0.10
    current_confidence_high: float = 85.0
    current_confidence_medium: float = 60.0

    # Tuning heuristics
    min_samples_for_tuning: int = 5
    fp_rate_warn: float = 0.25
    hallucination_rate_warn: float = 0.20
    decision_error_rate_warn: float = 0.30
    # When confidence looked high but anomaly was wrong → overconfident engine
    overconfidence_gap: float = 15.0  # conf - 50 when FP with conf ≥ 70

    # Recall proxy: fraction of high-severity incidents that got thumbs-up anomaly
    # (without ground-truth labels we can only *estimate*)


settings = Settings()
