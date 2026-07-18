"""12-factor settings for the grounded RCA engine."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "aiops-rca-engine"
    port: int = 8003
    log_level: str = "INFO"

    # Incident Manager (source of truth for tickets)
    incident_manager_url: str = "http://aiops-incident-manager:8002"

    # Observability backbone (grafana/otel-lgtm inside compose)
    prometheus_url: str = "http://lgtm:9090"
    loki_url: str = "http://lgtm:3100"
    tempo_url: str = "http://lgtm:3200"

    # Evidence window around the incident
    evidence_window_minutes: int = 15
    max_log_lines: int = 40
    max_traces: int = 15
    # Neighbor expansion (topology-aware RCA)
    # Env: TOPOLOGY_PATH — path to config/service_topology.yaml
    topology_path: str = ""
    # Env: RCA_PATTERNS_PATH — config/rca_patterns.yaml (config-driven rules)
    rca_patterns_path: str = ""
    enable_topology_expand: bool = True
    max_neighbor_log_lines: int = 15
    max_neighbor_traces: int = 8

    # Redis optional poll of new incidents (async path).
    # Default OFF: Decision Engine owns RCA invocation (single control plane).
    # Enable only if you intentionally want IM Redis fan-out → RCA without DE.
    redis_url: str = "redis://redis:6379/0"
    redis_queue_incidents: str = "aiops:incidents"
    enable_redis_poll: bool = False

    # Amazon Bedrock
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_default_region: str = "us-east-1"
    aws_session_token: str = ""
    # Default: Amazon Nova Lite (ON_DEMAND, no Anthropic use-case form).
    # Claude via inference profile also works after enabling model access, e.g.:
    #   us.anthropic.claude-sonnet-4-5-20250929-v1:0
    # Legacy anthropic.claude-3-5-sonnet-20240620-v1:0 is EOL on many accounts.
    bedrock_model_id: str = "amazon.nova-lite-v1:0"
    # Spec: 0.1–0.2 for grounded JSON stability (default mid-range)
    bedrock_temperature: float = 0.15
    bedrock_max_tokens: int = 2048
    bedrock_max_retries: int = 3
    bedrock_timeout_sec: float = 45.0
    # Force rule-based path (useful for CI / no AWS)
    force_rule_based: bool = False
    # If Bedrock confidence is below this, compare/use rule-based fallback
    min_bedrock_confidence: int = 40

    # Dedup: skip re-analysis if confidence already set within N minutes
    skip_if_analyzed_minutes: int = 5

    # After RCA succeeds, fan-out to Remediation (propose actions from suggested_actions)
    remediation_url: str = "http://aiops-remediation:8004"
    enable_remediation_fanout: bool = True
    remediation_timeout_sec: float = 15.0

    # Public Grafana URL as seen by the operator's browser (not the Docker DNS name).
    # Used to embed one-click Tempo deep-links on the incident ticket.
    grafana_public_url: str = "http://localhost:3000"
    # Display names for classic Explore `left=` deep-links (otel-lgtm defaults)
    tempo_datasource_name: str = "Tempo"
    loki_datasource_name: str = "Loki"
    tempo_datasource_uid: str = "tempo"
    loki_datasource_uid: str = "loki"


settings = Settings()
