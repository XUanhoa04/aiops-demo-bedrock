"""Engine QA business logic — enrich reviews from pipeline snapshots."""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.analytics import compute_quality
from app.clients import PipelineClients
from app.config import settings
from app.db import QARepository
from app.models import (
    EngineQualityMetrics,
    QADashboard,
    QAReview,
    QAReviewCreate,
    TuningAdvice,
)
from app.prom_metrics import record_review, refresh_gauges
from app.tuning import format_tuning_report, suggest_tuning

logger = logging.getLogger(__name__)


class EngineQAService:
    def __init__(
        self,
        repo: Optional[QARepository] = None,
        clients: Optional[PipelineClients] = None,
    ) -> None:
        self.repo = repo or QARepository()
        self.clients = clients or PipelineClients()
        refresh_gauges(self.quality())

    def close(self) -> None:
        self.clients.close()

    def submit(self, body: QAReviewCreate) -> QAReview:
        if not any(
            [
                body.anomaly_correct is not None,
                body.confidence_reasonable is not None,
                body.rca_useful is not None,
                body.decision_correct is not None,
                body.llm_hallucinated is not None,
                (body.comment or "").strip(),
                (body.corrected_root_cause or "").strip(),
            ]
        ):
            raise ValueError(
                "Provide at least one vote (anomaly/confidence/RCA/decision/"
                "hallucination) or a comment"
            )

        rec = QAReview(**body.model_dump())
        rec.reviewer = body.reviewer or settings.default_reviewer
        self._enrich_from_pipeline(rec)
        self.repo.insert(rec)

        quality = self.quality()
        record_review(rec, quality)

        if settings.sync_incident_manager:
            text = self._format_feedback(rec)
            self.clients.patch_incident_feedback(rec.incident_id, text)

        if settings.sync_feedback_collector:
            self.clients.push_feedback_collector(
                incident_id=rec.incident_id,
                anomaly_correct=rec.anomaly_correct,
                rca_useful=rec.rca_useful,
                comment=rec.comment,
                reviewer=rec.reviewer,
                corrected_root_cause=rec.corrected_root_cause,
            )

        logger.warning(
            "qa.review id=%s incident=%s anomaly=%s conf_ok=%s rca=%s decision=%s "
            "hallu=%s engine_conf=%s iterations=%s",
            rec.id,
            rec.incident_id,
            rec.anomaly_correct,
            rec.confidence_reasonable,
            rec.rca_useful,
            rec.decision_correct,
            rec.is_hallucination,
            rec.engine_confidence,
            rec.decision_iterations,
        )
        return rec

    def _enrich_from_pipeline(self, rec: QAReview) -> None:
        """Fill snapshot fields from IM / decision-engine when missing."""
        inc = self.clients.get_incident(rec.incident_id)
        if inc:
            rec.service_name = rec.service_name or inc.get("service_name")
            rec.severity = rec.severity or inc.get("severity")
            rec.metric_name = rec.metric_name or inc.get("metric_name")
            ctx = inc.get("context") or {}
            if rec.engine_confidence is None:
                rec.engine_confidence = _f(
                    ctx.get("confidence_score")
                    or (ctx.get("decision_engine") or {}).get("confidence_score")
                )
            if not rec.confidence_breakdown:
                rec.confidence_breakdown = dict(
                    ctx.get("confidence_breakdown")
                    or (ctx.get("decision_engine") or {}).get("confidence_breakdown")
                    or {}
                )
            if not rec.missing_context:
                rec.missing_context = list(
                    ctx.get("missing_context")
                    or (ctx.get("decision_engine") or {}).get("missing_context")
                    or []
                )
            if not rec.detection_method:
                rec.detection_method = str(
                    ctx.get("detection_method")
                    or (inc.get("labels") or {}).get("detection_method")
                    or ""
                )
            de = ctx.get("decision_engine") or {}
            if rec.decision_action is None and de.get("action"):
                rec.decision_action = str(de["action"])
            if rec.decision_iterations is None and de.get("iteration_count") is not None:
                rec.decision_iterations = int(de["iteration_count"])
            if rec.llm_confidence is None and de.get("llm_confidence") is not None:
                rec.llm_confidence = _f(de.get("llm_confidence"))
            if not rec.anomaly_id:
                rec.anomaly_id = inc.get("source_anomaly_id")
            if rec.engine_confidence is None and inc.get("rca_confidence") is not None:
                # IM stores rca_confidence 0–1 sometimes
                rc = _f(inc.get("rca_confidence"))
                if rc is not None and rec.llm_confidence is None:
                    rec.llm_confidence = rc * 100 if rc <= 1 else rc

        # Decision engine recent list match
        if rec.decision_action is None or rec.decision_iterations is None:
            for d in self.clients.list_decisions(limit=40):
                if rec.decision_id and d.get("id") == rec.decision_id:
                    self._apply_decision(rec, d)
                    break
                if rec.incident_id and d.get("incident_id") == rec.incident_id:
                    self._apply_decision(rec, d)
                    break
                if rec.anomaly_id and d.get("anomaly_id") == rec.anomaly_id:
                    self._apply_decision(rec, d)
                    break

    @staticmethod
    def _apply_decision(rec: QAReview, d: dict[str, Any]) -> None:
        rec.decision_id = rec.decision_id or d.get("id")
        rec.decision_action = rec.decision_action or d.get("action")
        if rec.decision_iterations is None and d.get("iteration_count") is not None:
            rec.decision_iterations = int(d["iteration_count"])
        if rec.engine_confidence is None:
            rec.engine_confidence = _f(d.get("confidence_score"))
        if rec.llm_confidence is None:
            rec.llm_confidence = _f(d.get("llm_confidence"))
        if not rec.missing_context:
            rec.missing_context = list(d.get("missing_context") or [])
        if not rec.confidence_breakdown:
            rec.confidence_breakdown = dict(d.get("confidence_breakdown") or {})

    @staticmethod
    def _format_feedback(rec: QAReview) -> str:
        def t(v: Optional[bool], label: str) -> str:
            if v is None:
                return f"{label}=skip"
            return f"{label}={'👍' if v else '👎'}"

        parts = [
            "[engine-qa]",
            t(rec.anomaly_correct, "anomaly"),
            t(rec.confidence_reasonable, "confidence"),
            t(rec.rca_useful, "rca"),
            t(rec.decision_correct, "decision"),
            f"by={rec.reviewer}",
        ]
        if rec.llm_hallucinated is not None:
            parts.append(f"hallucination={'yes' if rec.llm_hallucinated else 'no'}")
        if rec.engine_confidence is not None:
            parts.append(f"engine_conf={rec.engine_confidence:.0f}")
        if rec.decision_iterations is not None:
            parts.append(f"iters={rec.decision_iterations}")
        if rec.comment:
            parts.append(f"comment={rec.comment}")
        if rec.corrected_root_cause:
            parts.append(f"corrected_rca={rec.corrected_root_cause}")
        return " | ".join(parts)

    def list_reviews(self, limit: int = 50) -> list[QAReview]:
        return self.repo.list_reviews(limit=limit)

    def quality(self) -> EngineQualityMetrics:
        q = compute_quality(self.repo.list_all())
        refresh_gauges(q)
        return q

    def tuning(self) -> TuningAdvice:
        rows = self.repo.list_all()
        q = compute_quality(rows)
        return suggest_tuning(rows, q)

    def tuning_report(self) -> str:
        rows = self.repo.list_all()
        q = compute_quality(rows)
        advice = suggest_tuning(rows, q)
        return format_tuning_report(advice, q)

    def dashboard(self) -> QADashboard:
        rows = self.repo.list_all()
        q = compute_quality(rows)
        advice = suggest_tuning(rows, q)
        refresh_gauges(q)
        return QADashboard(
            quality=q,
            tuning=advice,
            recent_reviews=rows[:20],
            pipeline_status=self.clients.probe(),
        )

    def review_bundle(self, limit: int = 20) -> list[dict[str, Any]]:
        """
        Incidents + latest decision snapshot for the Streamlit review UI.
        """
        incidents = self.clients.list_incidents(limit=limit)
        decisions = self.clients.list_decisions(limit=50)
        by_inc = {
            d.get("incident_id"): d for d in decisions if d.get("incident_id")
        }
        reviewed = {r.incident_id for r in self.repo.list_reviews(limit=500)}
        out: list[dict[str, Any]] = []
        for inc in incidents:
            iid = str(inc.get("id") or "")
            d = by_inc.get(iid) or {}
            ctx = inc.get("context") or {}
            de = ctx.get("decision_engine") or d
            out.append(
                {
                    "incident": inc,
                    "decision": d or de,
                    "already_reviewed": iid in reviewed,
                    "engine_confidence": ctx.get("confidence_score")
                    or d.get("confidence_score")
                    or de.get("confidence_score"),
                    "missing_context": ctx.get("missing_context")
                    or d.get("missing_context")
                    or [],
                }
            )
        return out


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
