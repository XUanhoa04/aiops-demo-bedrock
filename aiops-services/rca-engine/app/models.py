"""RCA request / response schemas (strict structured output for ops)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class RCAResult(BaseModel):
    """
    Strict JSON contract required from the LLM (and from rule-based fallback).

    confidence is 0–100 (ops-friendly percent), not 0–1.

    why_root_cause forces *explainability*: the model must justify the claim
    from cited evidence, not just name a service.
    """

    root_cause: str = Field(..., description="Single best grounded root-cause hypothesis")
    why_root_cause: str = Field(
        default="",
        description="Explain why this is the root cause, citing concrete evidence facts",
    )
    confidence: int = Field(..., ge=0, le=100, description="Confidence percent 0-100")
    affected_components: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    runbook_suggestion: str = ""
    primary_trace_id: Optional[str] = Field(
        default=None,
        description="Best trace id from EVIDENCE to deep-link in Grafana Tempo",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            f = float(v)
        except (TypeError, ValueError):
            return 0
        if 0.0 <= f <= 1.0:
            f = f * 100.0
        return int(max(0, min(100, round(f))))

    @field_validator("affected_components", "evidence", "suggested_actions", mode="before")
    @classmethod
    def _ensure_list(cls, v: Any) -> list:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    @field_validator("why_root_cause", mode="before")
    @classmethod
    def _coerce_why(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)


class LLMUsage(BaseModel):
    """Token + latency telemetry for cost/SLO dashboards."""

    model_id: str = ""
    latency_ms: float = 0.0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_read_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    stop_reason: Optional[str] = None
    temperature: float = 0.15
    attempt: int = 1


class EvidencePack(BaseModel):
    """Grounded context gathered from Prom/Loki/Tempo + topology + incident."""

    incident_id: str
    service_name: str
    window_minutes: int
    window_start_iso: str
    window_end_iso: str
    incident: dict[str, Any] = Field(default_factory=dict)
    metrics_summary: dict[str, Any] = Field(default_factory=dict)
    error_logs: list[dict[str, Any]] = Field(default_factory=list)
    traces: list[dict[str, Any]] = Field(default_factory=list)
    gather_errors: list[str] = Field(default_factory=list)
    sources_ok: dict[str, bool] = Field(default_factory=dict)
    # Convenience: best trace id extracted during gather (logs or Tempo)
    primary_trace_id: Optional[str] = None

    # Topology-aware RCA fields
    # neighborhood: upstream (depends_on), downstream (callers), shared_deps
    topology: dict[str, Any] = Field(default_factory=dict)
    # Per-neighbor RED instant metrics {svc: {http_error_rate, ...}}
    neighbor_metrics: dict[str, Any] = Field(default_factory=dict)
    # Error logs collected from neighbor services (tagged with labels.service_name)
    neighbor_logs: list[dict[str, Any]] = Field(default_factory=list)
    # Traces where root_service is a neighbor
    neighbor_traces: list[dict[str, Any]] = Field(default_factory=list)
    # Best-effort change/chaos markers derived from logs
    change_events: list[dict[str, Any]] = Field(default_factory=list)

    def to_prompt_block(self, max_chars: int = 14000) -> str:
        """Compact, LLM-friendly dump of only grounded facts (incl. topology)."""
        import json

        payload = {
            "incident": {
                "id": self.incident.get("id"),
                "title": self.incident.get("title"),
                "description": (self.incident.get("description") or "")[:1500],
                "status": self.incident.get("status"),
                "severity": self.incident.get("severity"),
                "service_name": self.service_name,
                "metric_name": self.incident.get("metric_name"),
                "metric_value": self.incident.get("metric_value"),
                "threshold": self.incident.get("threshold"),
                "labels": self.incident.get("labels") or {},
                "anomaly_details": (self.incident.get("context") or {}).get(
                    "anomaly_details"
                ),
                "explanation": (self.incident.get("context") or {}).get("explanation"),
                "created_at": self.incident.get("created_at"),
            },
            "time_window": {
                "minutes": self.window_minutes,
                "start": self.window_start_iso,
                "end": self.window_end_iso,
            },
            # Service graph — prefer dependency root when neighbor is sicker
            "topology": self.topology,
            "metrics_summary": self.metrics_summary,
            "neighbor_metrics": self.neighbor_metrics,
            # Logs may include extracted trace_id — model must cite them in evidence[]
            "error_logs": self.error_logs[:40],
            "neighbor_logs": self.neighbor_logs[:25],
            "traces": self.traces[:15],
            "neighbor_traces": self.neighbor_traces[:10],
            "change_events": self.change_events[:15],
            "primary_trace_id_hint": self.primary_trace_id,
            "gather_errors": self.gather_errors,
            "sources_ok": self.sources_ok,
            "topology_guidance": (
                "upstream = services this service *calls* (dependencies). "
                "If an upstream has higher error_rate/latency and matching error logs, "
                "prefer that dependency as root_cause — the ticket service may be a symptom."
            ),
        }
        text = json.dumps(payload, indent=2, default=str)
        if len(text) > max_chars:
            return text[: max_chars - 20] + "\n…[truncated]"
        return text


class AnalyzeResponse(BaseModel):
    incident_id: str
    status: Literal["ok", "partial", "fallback", "error"]
    mode: Literal["bedrock", "rule_based", "skipped"]
    result: Optional[RCAResult] = None
    evidence_sources: dict[str, bool] = Field(default_factory=dict)
    bedrock_error: Optional[str] = None
    persisted: bool = False
    message: str = ""
    primary_trace_id: Optional[str] = None
    grafana_trace_url: Optional[str] = None
    llm_usage: Optional[LLMUsage] = None
