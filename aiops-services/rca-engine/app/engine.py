"""
RCA orchestration: gather evidence → Bedrock (or rules) → persist + trace link.

Pipeline
--------
  incident_id
      │
      ▼
  Incident Manager GET /incidents/{id}
      │
      ▼
  EvidenceGatherer (Prom metrics + Loki error logs w/ trace_id + Tempo traces)
      │
      ▼
  Bedrock converse()  ──on error OR low confidence──►  rule_based_rca()
      │
      ▼
  PATCH incident (root_cause, why, confidence, primary_trace_id, grafana URL)
  POST remediation /remediate/propose
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from aiops_shared.grafana_links import grafana_explore_trace_url

from app.bedrock_client import BedrockError, BedrockRCAClient
from app.config import settings
from app.evidence import EvidenceGatherer
from app.incident_client import IncidentClient
from app.models import AnalyzeResponse, EvidencePack, LLMUsage, RCAResult
from app.rule_fallback import rule_based_rca

logger = logging.getLogger(__name__)


class RCAEngine:
    def __init__(
        self,
        incidents: Optional[IncidentClient] = None,
        gatherer: Optional[EvidenceGatherer] = None,
        bedrock: Optional[BedrockRCAClient] = None,
    ) -> None:
        self.incidents = incidents or IncidentClient()
        self.gatherer = gatherer or EvidenceGatherer()
        self.bedrock = bedrock or BedrockRCAClient()
        self.analyzed = 0
        self.last_error: Optional[str] = None
        self.remediation_pushed = 0
        self._http = httpx.Client(
            timeout=httpx.Timeout(settings.remediation_timeout_sec, connect=3.0)
        )

    def close(self) -> None:
        self.incidents.close()
        self.gatherer.close()
        self._http.close()

    def analyze_incident(
        self,
        incident_id: str,
        *,
        persist: bool = True,
        force: bool = False,
        force_rule_based: bool = False,
    ) -> AnalyzeResponse:
        try:
            incident = self.incidents.get_incident(incident_id)
        except LookupError:
            return AnalyzeResponse(
                incident_id=incident_id,
                status="error",
                mode="rule_based",
                message="incident not found",
            )
        except Exception as exc:
            self.last_error = str(exc)
            return AnalyzeResponse(
                incident_id=incident_id,
                status="error",
                mode="rule_based",
                message=f"failed to fetch incident: {exc}",
            )

        if not force and self._recently_analyzed(incident):
            notes_tid = _trace_from_notes(incident.get("remediation_notes"))
            return AnalyzeResponse(
                incident_id=incident_id,
                status="ok",
                mode="skipped",
                message="already has recent RCA; pass force=true to re-run",
                result=RCAResult(
                    root_cause=incident.get("root_cause") or "previous RCA",
                    why_root_cause="Skipped re-analysis (recent RCA present).",
                    confidence=int(
                        round(float(incident.get("rca_confidence") or 0) * 100)
                    ),
                    affected_components=[incident.get("service_name") or ""],
                    evidence=["skipped re-analysis"],
                    suggested_actions=[],
                    runbook_suggestion="",
                    primary_trace_id=notes_tid,
                ),
                primary_trace_id=notes_tid,
                grafana_trace_url=_grafana_url(notes_tid),
                persisted=False,
            )

        pack = self.gatherer.gather(incident)
        result, mode, bedrock_error, llm_usage = self._run_model(
            pack, force_rule_based=force_rule_based
        )

        # Prefer model/rule-selected trace, else pack primary
        primary_trace_id = (
            result.primary_trace_id or pack.primary_trace_id
        )
        result.primary_trace_id = primary_trace_id
        grafana_url = _grafana_url(primary_trace_id)

        persisted = False
        if persist:
            try:
                self.incidents.persist_rca(
                    incident_id,
                    result,
                    mode=mode,
                    primary_trace_id=primary_trace_id,
                    service_name=pack.service_name,
                    related_traces=pack.traces,
                    grafana_trace_url=grafana_url,
                    extra_notes={
                        "sources_ok": pack.sources_ok,
                        "gather_errors": pack.gather_errors,
                        "window": {
                            "start": pack.window_start_iso,
                            "end": pack.window_end_iso,
                        },
                        "metrics_summary": pack.metrics_summary.get("instant")
                        if isinstance(pack.metrics_summary, dict)
                        else {},
                        "llm_usage": llm_usage.model_dump() if llm_usage else None,
                        "why_root_cause": result.why_root_cause,
                    },
                )
                persisted = True
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("persist RCA failed: %s", exc)
                return AnalyzeResponse(
                    incident_id=incident_id,
                    status="partial",
                    mode=mode,  # type: ignore[arg-type]
                    result=result,
                    evidence_sources=pack.sources_ok,
                    bedrock_error=bedrock_error,
                    persisted=False,
                    message=f"RCA computed but persist failed: {exc}",
                    primary_trace_id=primary_trace_id,
                    grafana_trace_url=grafana_url,
                    llm_usage=llm_usage,
                )

        if persisted:
            self._fanout_remediation(incident_id, result)

        self.analyzed += 1
        status = "ok" if mode == "bedrock" else "fallback"
        if pack.gather_errors and mode == "bedrock":
            status = "partial"

        logger.info(
            "RCA complete incident=%s mode=%s confidence=%s primary_trace_id=%s "
            "grafana_url=%s llm_latency_ms=%s",
            incident_id,
            mode,
            result.confidence,
            primary_trace_id,
            grafana_url,
            llm_usage.latency_ms if llm_usage else None,
        )

        return AnalyzeResponse(
            incident_id=incident_id,
            status=status,  # type: ignore[arg-type]
            mode=mode,  # type: ignore[arg-type]
            result=result,
            evidence_sources=pack.sources_ok,
            bedrock_error=bedrock_error,
            persisted=persisted,
            message="RCA complete",
            primary_trace_id=primary_trace_id,
            grafana_trace_url=grafana_url,
            llm_usage=llm_usage,
        )

    def _fanout_remediation(self, incident_id: str, result: RCAResult) -> None:
        if not settings.enable_remediation_fanout or not settings.remediation_url:
            return
        url = f"{settings.remediation_url.rstrip('/')}/remediate/propose"
        body = {
            "incident_id": incident_id,
            "actions": list(result.suggested_actions or []),
            "auto_execute_low_risk": True,
        }
        try:
            resp = self._http.post(url, json=body)
            if resp.status_code >= 400:
                logger.warning(
                    "remediation fan-out HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return
            self.remediation_pushed += 1
            logger.info(
                "remediation proposed for incident=%s actions=%s",
                incident_id,
                len(result.suggested_actions or []),
            )
        except Exception as exc:
            logger.warning("remediation fan-out failed: %s", exc)

    def analyze_from_payload(
        self,
        incident: dict[str, Any],
        *,
        persist: bool = True,
        force: bool = True,
    ) -> AnalyzeResponse:
        iid = str(incident.get("id") or "")
        if not iid:
            return AnalyzeResponse(
                incident_id="",
                status="error",
                mode="rule_based",
                message="incident payload missing id",
            )
        try:
            return self.analyze_incident(iid, persist=persist, force=force)
        except Exception:
            pack = self.gatherer.gather(incident)
            result, mode, bedrock_error, llm_usage = self._run_model(pack)
            tid = result.primary_trace_id or pack.primary_trace_id
            return AnalyzeResponse(
                incident_id=iid,
                status="fallback" if mode != "bedrock" else "ok",
                mode=mode,  # type: ignore[arg-type]
                result=result,
                evidence_sources=pack.sources_ok,
                bedrock_error=bedrock_error,
                persisted=False,
                message="analyzed from payload without manager refresh",
                primary_trace_id=tid,
                grafana_trace_url=_grafana_url(tid),
                llm_usage=llm_usage,
            )

    def _run_model(
        self,
        pack: EvidencePack,
        *,
        force_rule_based: bool = False,
    ) -> tuple[RCAResult, str, Optional[str], Optional[LLMUsage]]:
        """
        Returns (result, mode, bedrock_error, llm_usage).

        Fallback triggers:
          1. FORCE_RULE_BASED env or force_rule_based=True (eval / ops)
          2. missing AWS credentials
          3. Bedrock exception
          4. Bedrock confidence < min_bedrock_confidence (use rules if better)
        """
        if settings.force_rule_based or force_rule_based:
            return (
                rule_based_rca(pack),
                "rule_based",
                "force_rule_based=true",
                None,
            )

        if not self.bedrock.configured:
            logger.warning("Bedrock not configured — using rule-based RCA")
            return rule_based_rca(pack), "rule_based", "credentials missing", None

        try:
            result, usage = self.bedrock.analyze(pack)
            # Low-confidence safety: prefer rules if they are more confident
            if result.confidence < settings.min_bedrock_confidence:
                rules = rule_based_rca(pack)
                logger.warning(
                    "bedrock confidence=%s < min=%s — comparing rule-based conf=%s",
                    result.confidence,
                    settings.min_bedrock_confidence,
                    rules.confidence,
                )
                if rules.confidence >= result.confidence:
                    # Keep any model-cited trace id if rules lack one
                    if not rules.primary_trace_id:
                        rules.primary_trace_id = result.primary_trace_id
                    return (
                        rules,
                        "rule_based",
                        f"low_confidence={result.confidence}",
                        usage,
                    )
                # Keep bedrock but flag in error field for transparency
                return (
                    result,
                    "bedrock",
                    f"low_confidence_kept={result.confidence}",
                    usage,
                )
            return result, "bedrock", None, usage
        except BedrockError as exc:
            logger.warning("Bedrock failed, falling back to rules: %s", exc)
            return rule_based_rca(pack), "rule_based", str(exc), self.bedrock.last_usage

    @staticmethod
    def _recently_analyzed(incident: dict[str, Any]) -> bool:
        if not incident.get("root_cause"):
            return False
        if incident.get("rca_confidence") is None:
            return False
        updated = incident.get("updated_at")
        if not updated:
            return True
        try:
            if isinstance(updated, str):
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            else:
                return True
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            return age < settings.skip_if_analyzed_minutes * 60
        except Exception:
            return True

    def status(self) -> dict[str, Any]:
        return {
            "analyzed": self.analyzed,
            "last_error": self.last_error,
            "bedrock": self.bedrock.status(),
            "observability": self.gatherer.probe(),
            "incident_manager_ok": self.incidents.healthy(),
            "remediation_url": settings.remediation_url or None,
            "remediation_fanout": settings.enable_remediation_fanout,
            "remediation_pushed": self.remediation_pushed,
            "min_bedrock_confidence": settings.min_bedrock_confidence,
            "grafana_public_url": settings.grafana_public_url,
        }


def _grafana_url(trace_id: Optional[str]) -> Optional[str]:
    if not trace_id:
        return None
    return grafana_explore_trace_url(
        grafana_base=settings.grafana_public_url,
        trace_id=trace_id,
        datasource_name=settings.tempo_datasource_name,
        datasource_uid=settings.tempo_datasource_uid,
    )


def _trace_from_notes(raw: Any) -> Optional[str]:
    import json

    if not raw:
        return None
    if isinstance(raw, dict):
        return raw.get("primary_trace_id")
    try:
        if str(raw).strip().startswith("{"):
            return json.loads(raw).get("primary_trace_id")
    except Exception:
        return None
    return None
