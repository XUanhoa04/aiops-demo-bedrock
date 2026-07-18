"""
12-factor settings for the Decision Engine.

Thresholds are env-tunable so SRE can hot-tune without rebuilds.
See decision_table.py for the full policy matrix and rationale.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str = "aiops-decision-engine"
    port: int = 8006
    log_level: str = "INFO"

    # --- Downstream services ---
    anomaly_detector_url: str = "http://aiops-anomaly-detector:8001"
    incident_manager_url: str = "http://aiops-incident-manager:8002"
    rca_engine_url: str = "http://aiops-rca-engine:8003"
    remediation_url: str = "http://aiops-remediation:8004"

    # --- Decision thresholds (0–100 confidence from Confidence Scorer) ---
    # Env: CONFIDENCE_HIGH / CONFIDENCE_MEDIUM
    confidence_high: float = 85.0
    confidence_medium: float = 60.0

    # Missing any of these forces escalate even if confidence is high-ish.
    # Default: sufficient_metrics only (demo often lacks Tempo traces; add
    # "trace_id" in prod: CRITICAL_MISSING_CONTEXT=sufficient_metrics,trace_id).
    # Env: CRITICAL_MISSING_CONTEXT  (comma-separated)
    critical_missing_context: str = "sufficient_metrics"

    # --- Limited iteration loop ---
    # Spec: max 2–3 rounds; after that always handoff to on-call.
    # Env: MAX_ITERATIONS
    max_iterations: int = 3

    # On medium path: re-gather context before first LLM call when missing context
    enable_context_refresh: bool = True

    # --- LLM (via RCA engine — only for MEDIUM band) ---
    # Env: ENABLE_LLM / RCA_WAIT / RCA_MAX_TOKENS_HINT (documented; RCA owns token budget)
    enable_llm: bool = True
    rca_wait: bool = True  # Decision Engine needs structured result, not async queue
    rca_force: bool = True
    # If LLM own-confidence below this, escalate rather than trust suggestions
    min_llm_confidence: float = 40.0

    # --- Auto-remediation (HIGH band) — GATED ---
    # Never force-execute high-risk actions. We only:
    #   1) match known safe patterns
    #   2) log the decision
    #   3) propose via remediation API with auto_execute_low_risk=false
    #      unless AUTO_EXECUTE_GATED_LOW_RISK is explicitly true
    enable_auto_remediation: bool = True
    auto_execute_gated_low_risk: bool = False  # default: propose only (human gate)

    # --- Escalation ---
    escalate_severity: str = "high"
    escalate_status: str = "investigating"

    # --- Redis consumer (optional secondary path) ---
    redis_url: str = "redis://redis:6379/0"
    # Dedicated queue so we do not race Incident Manager on aiops:anomalies.
    # Detector can dual-publish later; for now POST /decide + optional fan-out.
    redis_queue_decisions: str = "aiops:decisions"
    enable_redis_consumer: bool = True
    # Also listen to anomaly queue copy if set (empty = disabled)
    redis_queue_anomalies_mirror: str = ""

    # Persist decision trail onto incident.context via PATCH
    patch_incident_context: bool = True

    @property
    def critical_missing_set(self) -> set[str]:
        return {
            s.strip()
            for s in self.critical_missing_context.split(",")
            if s.strip()
        }


settings = Settings()
