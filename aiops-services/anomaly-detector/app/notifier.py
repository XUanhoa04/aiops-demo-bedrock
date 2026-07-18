"""
Notify downstream when an anomaly is confirmed *and* passes confidence gate.

Channels
--------
1. Redis LIST queue (primary async path → incident-manager consumer)
2. HTTP webhook POST (sync path → incident-manager /incidents/from-anomaly)

Both are env-toggleable. Failures are logged and counted; they never crash the
poll loop (at-least-once best-effort for demos; prod needs DLQ + retries).

Decision Engine contract
------------------------
We publish AnomalyEvent (shared schema) whose `context` embeds:
  confidence_score, confidence_breakdown, missing_context, context_completeness,
  signals, explanation, anomaly_score, detection_method.

Full DetectionDecision is also returned by the API (`/decisions`).
"""

from __future__ import annotations

import logging

import httpx

from aiops_shared.models import AnomalyEvent
from aiops_shared.redis_client import enqueue, get_redis, ping

from app.config import settings
from app.models import DetectionDecision
from app.prom_metrics import ANOMALIES_EMITTED, ERRORS_TOTAL

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self) -> None:
        self.redis = get_redis(settings.redis_url)
        self._http = httpx.Client(timeout=5.0)

    def close(self) -> None:
        self._http.close()

    def redis_ok(self) -> bool:
        return ping(self.redis)

    def publish_decision(self, decision: DetectionDecision, event: AnomalyEvent) -> AnomalyEvent:
        """Publish a pre-built AnomalyEvent derived from DetectionDecision."""
        if decision.confidence_score < settings.min_confidence_to_notify:
            logger.info(
                "skip notify: confidence %.1f < min %.1f service=%s metric=%s",
                decision.confidence_score,
                settings.min_confidence_to_notify,
                decision.service_name,
                decision.metric_name,
            )
            return event

        method_label = event.labels.get("detection_method", "hybrid")

        if settings.enable_redis_notify:
            try:
                enqueue(
                    self.redis,
                    settings.redis_queue_anomalies,
                    event.to_redis_json(),
                )
                logger.warning(
                    "anomaly→redis id=%s service=%s metric=%s score=%.3f "
                    "confidence=%.1f methods=%s missing=%s",
                    event.id,
                    event.service_name,
                    event.metric_name,
                    decision.anomaly_score,
                    decision.confidence_score,
                    decision.detection_methods,
                    decision.missing_context,
                )
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="redis").inc()
                logger.exception("redis notify failed: %s", exc)

        # Fan-out to Decision Engine queue (aiops:decisions) — separate from IM
        if settings.enable_decision_queue and settings.redis_queue_decisions:
            try:
                enqueue(
                    self.redis,
                    settings.redis_queue_decisions,
                    event.to_redis_json(),
                )
                logger.info(
                    "anomaly→decision-queue id=%s confidence=%.1f",
                    event.id,
                    decision.confidence_score,
                )
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="decision_queue").inc()
                logger.warning("decision queue notify failed: %s", exc)

        if settings.enable_webhook_notify and settings.incident_webhook_url:
            try:
                resp = self._http.post(
                    settings.incident_webhook_url,
                    content=event.model_dump_json(),
                    headers={"Content-Type": "application/json"},
                )
                if resp.status_code >= 400:
                    ERRORS_TOTAL.labels(stage="webhook").inc()
                    logger.error(
                        "webhook notify HTTP %s body=%s",
                        resp.status_code,
                        resp.text[:300],
                    )
                else:
                    logger.info(
                        "anomaly→webhook id=%s status=%s confidence=%.1f",
                        event.id,
                        resp.status_code,
                        decision.confidence_score,
                    )
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="webhook").inc()
                logger.warning("webhook notify failed: %s", exc)

        ANOMALIES_EMITTED.labels(
            service=decision.service_name,
            metric=decision.metric_name,
            method=method_label,
        ).inc()
        return event
