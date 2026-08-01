"""Redis anomaly consumer → create / correlate incidents."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiops_shared.models import AnomalyEvent, Incident
from aiops_shared.redis_client import (
    acknowledge,
    enqueue,
    get_redis,
    ping,
    recover_inflight,
    reserve,
    retry_or_dead_letter,
)

from app.config import settings
from app.db import IncidentRepository, incident_from_anomaly
from app.decision_client import DecisionClient
from app.prom_metrics import ERRORS_TOTAL, record_correlated, record_created, set_open_incidents
from app.rca_client import RCAClient

logger = logging.getLogger(__name__)


class AnomalyConsumer:
    def __init__(
        self,
        repo: IncidentRepository,
        rca: Optional[RCAClient] = None,
        decision: Optional[DecisionClient] = None,
    ) -> None:
        self.repo = repo
        self.rca = rca or RCAClient()
        self.decision = decision or DecisionClient()
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.processed = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        self._stop.clear()
        self._refresh_open_gauge()
        recovered = await asyncio.to_thread(
            recover_inflight,
            self.redis,
            queue=settings.redis_queue_anomalies,
            processing_queue=settings.redis_queue_anomalies_processing,
        )
        self._task = asyncio.create_task(self._run(), name="anomaly-consumer")
        logger.info(
            "consumer started queue=%s recovered=%s",
            settings.redis_queue_anomalies,
            recovered,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        logger.info("consumer stopped processed=%s", self.processed)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.to_thread(
                    reserve,
                    self.redis,
                    settings.redis_queue_anomalies,
                    settings.redis_queue_anomalies_processing,
                    2,
                )
                if raw is None:
                    continue
                try:
                    await asyncio.to_thread(self._handle_payload, raw)
                except Exception as exc:
                    disposition = await asyncio.to_thread(
                        retry_or_dead_letter,
                        self.redis,
                        queue=settings.redis_queue_anomalies,
                        processing_queue=settings.redis_queue_anomalies_processing,
                        dead_letter_queue=settings.redis_queue_anomalies_dlq,
                        payload=raw,
                        error=str(exc),
                        max_retries=settings.queue_max_retries,
                    )
                    logger.exception(
                        "anomaly processing failed disposition=%s: %s",
                        disposition,
                        exc,
                    )
                    raise
                await asyncio.to_thread(
                    acknowledge,
                    self.redis,
                    settings.redis_queue_anomalies_processing,
                    raw,
                )
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                ERRORS_TOTAL.labels(stage="consumer").inc()
                logger.exception("consumer error: %s", exc)
                await asyncio.sleep(1)

    def _handle_payload(self, raw: str) -> Incident:
        anomaly = AnomalyEvent.from_redis_json(raw)
        return self.handle_anomaly(anomaly, source="redis")

    def handle_anomaly(
        self,
        anomaly: AnomalyEvent,
        *,
        source: str = "redis",
    ) -> Incident:
        """Create or correlate an incident from an anomaly event."""
        already_processed = self.repo.get_by_anomaly_id(anomaly.id)
        if already_processed:
            logger.info(
                "duplicate anomaly ignored anomaly=%s incident=%s",
                anomaly.id,
                already_processed.id,
            )
            return already_processed

        existing = self.repo.find_open_correlated(
            service_name=anomaly.service_name,
            metric_name=anomaly.metric_name,
            window_minutes=settings.correlation_window_minutes,
        )
        if existing:
            # Update metric snapshot; keep single ticket (noise reduction)
            existing.metric_value = anomaly.metric_value
            existing.context = {
                **existing.context,
                "last_anomaly_id": anomaly.id,
                "occurrence_count": int(existing.context.get("occurrence_count", 1)) + 1,
                # anomaly_details keeps the latest detector payload for UI/RCA
                "anomaly_details": {
                    "anomaly_id": anomaly.id,
                    "metric_name": anomaly.metric_name,
                    "metric_value": anomaly.metric_value,
                    "threshold": anomaly.threshold,
                    "detector": anomaly.detector,
                    "message": anomaly.message,
                    "labels": anomaly.labels,
                    "context": anomaly.context,
                    "detected_at": anomaly.detected_at.isoformat(),
                },
            }
            existing.description = (
                f"{existing.description}\n---\n{anomaly.message}".strip()
            )
            if anomaly.severity.value == "critical":
                existing.severity = anomaly.severity
            existing, updated = self.repo.update_for_anomaly(existing, anomaly.id)
            if not updated:
                return existing
            record_correlated(anomaly.service_name)
            self._refresh_open_gauge()
            logger.info(
                "correlated anomaly=%s → incident=%s count=%s",
                anomaly.id,
                existing.id,
                existing.context.get("occurrence_count"),
            )
            self.processed += 1
            return existing

        incident = incident_from_anomaly(anomaly)
        incident, created = self.repo.insert_for_anomaly(incident, anomaly.id)
        if not created:
            return incident
        record_created(source=source, severity=incident.severity.value, service=incident.service_name)
        self._refresh_open_gauge()

        self.fanout_new_incident(incident, anomaly=anomaly)

        logger.warning(
            "incident created id=%s title=%s severity=%s source=%s",
            incident.id,
            incident.title,
            incident.severity.value,
            source,
        )
        self.processed += 1
        return incident

    def fanout_new_incident(
        self,
        incident: Incident,
        anomaly: Optional[AnomalyEvent] = None,
    ) -> None:
        """
        Single control-plane fan-out after a new ticket is created.

        Primary path
        ------------
        Decision Engine only → policy selects escalate / gated remediate / RCA.
        RCA (Bedrock) is invoked *by* Decision Engine on medium/high-no-pattern
        bands — not unconditionally from Incident Manager.

        Legacy / emergency
        ------------------
        ENABLE_DIRECT_RCA_FANOUT=true restores IM → RCA HTTP (dual path; costlier).
        If Decision Engine is disabled, we fall back to direct RCA so demos
        still get root_cause without a silent black hole.
        """
        if settings.enable_redis_incident_fanout:
            try:
                enqueue(
                    self.redis,
                    settings.redis_queue_incidents,
                    incident.model_dump_json(),
                )
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="redis").inc()
                logger.warning("incident fan-out failed: %s", exc)

        decision_ok = False
        if self.decision.enabled:
            result = self.decision.push(incident, anomaly=anomaly)
            decision_ok = bool(result.get("ok"))

        # Direct RCA only when explicitly enabled, or when DE is unavailable
        # so the pipeline still produces RCA without dual-calling both.
        want_direct_rca = settings.enable_direct_rca_fanout or (
            not self.decision.enabled and self.rca.enabled
        )
        if want_direct_rca and self.rca.enabled:
            logger.info(
                "direct RCA fan-out incident=%s reason=%s",
                incident.id,
                "RCA_ALWAYS_ON"
                if settings.enable_direct_rca_fanout
                else "decision_engine_disabled",
            )
            self.rca.push_incident(incident)
        elif not decision_ok and not self.decision.enabled:
            logger.warning(
                "no control plane for incident=%s (decision off, direct RCA off)",
                incident.id,
            )

    def _refresh_open_gauge(self) -> None:
        try:
            set_open_incidents(self.repo.count_open())
        except Exception as exc:
            ERRORS_TOTAL.labels(stage="metrics").inc()
            logger.warning("open_incidents gauge refresh failed: %s", exc)

    def status(self) -> dict:
        return {
            "processed": self.processed,
            "redis_ok": ping(self.redis),
            "last_error": self.last_error,
            "queue": settings.redis_queue_anomalies,
            "processing_queue": settings.redis_queue_anomalies_processing,
            "dead_letter_queue": settings.redis_queue_anomalies_dlq,
            "rca": self.rca.status(),
            "decision": self.decision.status(),
        }
