"""Business logic: propose from RCA, approve, execute, false-positive."""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.classifier import classify_action, classify_many
from app.config import settings
from app.db import ActionRepository
from app.executor import ActionExecutor, risk_allows_auto
from app.incident_client import IncidentClient
from app.models import (
    ActionRecord,
    ActionStatus,
    ActionType,
    IncidentBundle,
    RiskLevel,
    utc_now,
)

logger = logging.getLogger(__name__)


class RemediationService:
    def __init__(
        self,
        repo: Optional[ActionRepository] = None,
        incidents: Optional[IncidentClient] = None,
        executor: Optional[ActionExecutor] = None,
    ) -> None:
        self.repo = repo or ActionRepository()
        self.incidents = incidents or IncidentClient()
        self.executor = executor or ActionExecutor()

    def close(self) -> None:
        self.incidents.close()
        self.executor.close()

    # ------------------------------------------------------------------
    # Propose from RCA suggested_actions
    # ------------------------------------------------------------------

    def propose_for_incident(
        self,
        incident_id: str,
        actions: Optional[list[str]] = None,
        *,
        auto_execute_low_risk: Optional[bool] = None,
    ) -> list[ActionRecord]:
        incident = self.incidents.get_incident(incident_id)
        rca = self.incidents.extract_rca(incident)
        texts = actions if actions else list(rca.get("suggested_actions") or [])
        if not texts:
            # Fallback: synthesize one low-risk chaos reset for demo services
            svc = incident.get("service_name") or "checkout-service"
            texts = [
                f"Reset error_rate chaos on {svc}",
                f"Investigate logs for {svc}",
                f"Restart service {svc}",
            ]

        default_svc = incident.get("service_name") or ""
        classified = classify_many(texts, default_service=default_svc)
        auto = (
            settings.auto_execute_low_risk
            if auto_execute_low_risk is None
            else auto_execute_low_risk
        )

        created: list[ActionRecord] = []
        for c in classified:
            payload: dict = {}
            if c.replicas is not None:
                payload["replicas"] = c.replicas
            rec = ActionRecord(
                incident_id=incident_id,
                action_type=c.action_type.value,
                action_text=c.action_text,
                target_service=c.target_service,
                risk_level=c.risk_level,
                status=ActionStatus.PROPOSED,
                payload=payload,
                result=c.reason,
                executed_by=None,
            )
            self.repo.insert(rec)

            if auto and risk_allows_auto(rec) and rec.action_type != ActionType.LOG_ONLY.value:
                rec = self.executor.execute(rec, executed_by=settings.default_executor)
                self.repo.update(rec)
            elif auto and rec.action_type == ActionType.LOG_ONLY.value:
                rec = self.executor.execute(rec, executed_by=settings.default_executor)
                self.repo.update(rec)

            created.append(rec)
            logger.info(
                "proposed action id=%s risk=%s type=%s status=%s",
                rec.id,
                rec.risk_level.value,
                rec.action_type,
                rec.status.value,
            )
        return created

    # ------------------------------------------------------------------
    # Approve / execute / reject
    # ------------------------------------------------------------------

    def approve(
        self,
        action_id: str,
        *,
        executed_by: str,
        execute_now: bool = True,
    ) -> ActionRecord:
        rec = self.repo.get(action_id)
        if not rec:
            raise LookupError(action_id)
        if rec.status in (ActionStatus.EXECUTED, ActionStatus.SIMULATED):
            return rec
        rec.status = ActionStatus.APPROVED
        rec.executed_by = executed_by
        self.repo.update(rec)
        if execute_now:
            return self.execute(action_id, executed_by=executed_by, force=True)
        return rec

    def execute(
        self,
        action_id: str,
        *,
        executed_by: str,
        force: bool = False,
    ) -> ActionRecord:
        rec = self.repo.get(action_id)
        if not rec:
            raise LookupError(action_id)

        if rec.status in (ActionStatus.EXECUTED, ActionStatus.SIMULATED):
            return rec

        # High-risk requires approval unless force
        if rec.risk_level == RiskLevel.HIGH and rec.status != ActionStatus.APPROVED and not force:
            rec.result = "blocked: high-risk action requires approval"
            self.repo.update(rec)
            return rec

        rec = self.executor.execute(rec, executed_by=executed_by)
        self.repo.update(rec)

        # Reflect on incident ticket
        try:
            note = {
                "last_action_id": rec.id,
                "action_type": rec.action_type,
                "status": rec.status.value,
                "command": rec.command,
                "result": rec.result,
                "executed_by": rec.executed_by,
            }
            # Preserve prior RCA JSON if present
            incident = self.incidents.get_incident(rec.incident_id)
            prev = incident.get("remediation_notes") or ""
            merged = prev
            try:
                if prev.strip().startswith("{"):
                    obj = json.loads(prev)
                    obj["last_remediation"] = note
                    merged = json.dumps(obj, ensure_ascii=False)
                else:
                    merged = json.dumps(
                        {"prior": prev, "last_remediation": note},
                        ensure_ascii=False,
                    )
            except Exception:
                merged = json.dumps({"last_remediation": note}, ensure_ascii=False)

            status = "remediating"
            if rec.status in (ActionStatus.EXECUTED, ActionStatus.SIMULATED):
                # leave investigating/remediating — human closes after verify
                status = "remediating"
            self.incidents.patch_incident(
                rec.incident_id,
                {"status": status, "remediation_notes": merged[:4000]},
            )
        except Exception as exc:
            logger.warning("incident patch after execute failed: %s", exc)

        return rec

    def reject(self, action_id: str, *, executed_by: str, reason: str = "") -> ActionRecord:
        rec = self.repo.get(action_id)
        if not rec:
            raise LookupError(action_id)
        rec.status = ActionStatus.REJECTED
        rec.executed_by = executed_by
        rec.result = reason or "rejected by operator"
        rec.executed_at = utc_now()
        return self.repo.update(rec)

    def mark_false_positive(
        self,
        incident_id: str,
        *,
        executed_by: str,
        note: str,
    ) -> tuple[ActionRecord, dict]:
        rec = ActionRecord(
            incident_id=incident_id,
            action_type=ActionType.MARK_FALSE_POSITIVE.value,
            action_text=note,
            target_service="",
            risk_level=RiskLevel.LOW,
            status=ActionStatus.PROPOSED,
            executed_by=executed_by,
        )
        self.repo.insert(rec)
        rec = self.executor.execute(rec, executed_by=executed_by)
        self.repo.update(rec)
        incident = self.incidents.mark_false_positive(incident_id, note)
        return rec, incident

    # ------------------------------------------------------------------
    # Bundles for UI
    # ------------------------------------------------------------------

    def list_bundles(self, limit: int = 20) -> list[IncidentBundle]:
        incidents = self.incidents.list_incidents(limit=limit)
        bundles: list[IncidentBundle] = []
        for inc in incidents:
            rca = self.incidents.extract_rca(inc)
            history = self.repo.list(incident_id=inc.get("id"), limit=30)
            bundles.append(
                IncidentBundle(
                    incident=inc,
                    suggested_actions=list(rca.get("suggested_actions") or []),
                    rca=rca,
                    history=history,
                )
            )
        return bundles

    def get_bundle(self, incident_id: str) -> IncidentBundle:
        inc = self.incidents.get_incident(incident_id)
        rca = self.incidents.extract_rca(inc)
        history = self.repo.list(incident_id=incident_id, limit=50)
        return IncidentBundle(
            incident=inc,
            suggested_actions=list(rca.get("suggested_actions") or []),
            rca=rca,
            history=history,
        )
