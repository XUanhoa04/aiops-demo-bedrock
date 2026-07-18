"""Feedback business logic."""

from __future__ import annotations

import logging
from typing import Optional

from app.config import settings
from app.db import FeedbackRepository
from app.incident_client import IncidentClient
from app.metrics import record_submission, refresh_gauges
from app.models import FeedbackCreate, FeedbackRecord, FeedbackStats
from app.tuning import suggest_threshold_adjustments

logger = logging.getLogger(__name__)


class FeedbackService:
    def __init__(
        self,
        repo: Optional[FeedbackRepository] = None,
        incidents: Optional[IncidentClient] = None,
    ) -> None:
        self.repo = repo or FeedbackRepository()
        self.incidents = incidents or IncidentClient()
        # Seed gauges from DB on boot
        refresh_gauges(self.repo.compute_stats())

    def close(self) -> None:
        self.incidents.close()

    def submit(self, body: FeedbackCreate) -> FeedbackRecord:
        service_name = severity = incident_status = None
        try:
            inc = self.incidents.get_incident(body.incident_id)
            service_name = inc.get("service_name")
            severity = inc.get("severity")
            incident_status = inc.get("status")
        except LookupError:
            logger.warning("incident %s not found — storing feedback only", body.incident_id)
        except Exception as exc:
            logger.warning("incident fetch failed: %s", exc)

        rec = FeedbackRecord(
            incident_id=body.incident_id,
            anomaly_correct=body.anomaly_correct,
            rca_useful=body.rca_useful,
            action_effective=body.action_effective,
            comment=body.comment or "",
            reviewer=body.reviewer or settings.default_reviewer,
            corrected_root_cause=body.corrected_root_cause,
            service_name=service_name,
            severity=severity,
            incident_status=incident_status,
        )
        self.repo.insert(rec)
        stats = self.repo.compute_stats()
        record_submission(rec, stats)

        if settings.sync_incident_manager:
            try:
                text = self._format_human_feedback(rec)
                self.incidents.apply_feedback(
                    body.incident_id,
                    human_feedback=text,
                    mark_false_positive=rec.is_false_positive,
                )
            except LookupError:
                logger.warning("skip IM sync — incident missing")
            except Exception as exc:
                logger.warning("IM sync failed: %s", exc)

        logger.info(
            "feedback saved id=%s incident=%s fp=%s reviewer=%s",
            rec.id,
            rec.incident_id,
            rec.is_false_positive,
            rec.reviewer,
        )
        return rec

    @staticmethod
    def _format_human_feedback(rec: FeedbackRecord) -> str:
        def thumb(v: Optional[bool], label: str) -> str:
            if v is None:
                return f"{label}=skip"
            return f"{label}={'👍' if v else '👎'}"

        parts = [
            thumb(rec.anomaly_correct, "anomaly"),
            thumb(rec.rca_useful, "rca"),
            thumb(rec.action_effective, "action"),
            f"by={rec.reviewer}",
        ]
        if rec.comment:
            parts.append(f"comment={rec.comment}")
        if rec.corrected_root_cause:
            parts.append(f"corrected_rca={rec.corrected_root_cause}")
        return " | ".join(parts)

    def stats(self) -> FeedbackStats:
        stats = self.repo.compute_stats()
        refresh_gauges(stats)
        return stats

    def tuning(self):
        return suggest_threshold_adjustments(self.repo)
