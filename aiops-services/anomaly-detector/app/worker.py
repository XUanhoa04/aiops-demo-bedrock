"""Background poll loop: Prometheus → detect → Redis queue."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiops_shared.models import AnomalyEvent
from aiops_shared.redis_client import enqueue, get_redis, ping

from app.config import settings
from app.detector import AnomalyEngine
from app.prometheus_client import (
    PrometheusClient,
    query_error_rate,
    query_latency_p95,
)

logger = logging.getLogger(__name__)

# Services we actively watch in the demo
WATCHED_SERVICES = ("checkout-service", "payment-service")


class DetectorWorker:
    def __init__(self) -> None:
        self.engine = AnomalyEngine()
        self.prom = PrometheusClient()
        self.redis = get_redis(settings.redis_url)
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.recent_anomalies: list[AnomalyEvent] = []
        self.poll_count = 0
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="anomaly-poller")
        logger.info("detector worker started interval=%ss", settings.anomaly_poll_interval_sec)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=10)
        self.prom.close()
        logger.info("detector worker stopped")

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._poll_once)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                logger.exception("poll cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=settings.anomaly_poll_interval_sec,
                )
            except asyncio.TimeoutError:
                pass

    def _poll_once(self) -> None:
        self.poll_count += 1
        # Re-probe Prometheus each cycle (LGTM may become ready after us).
        self.prom.reset_unreachable()
        for svc in WATCHED_SERVICES:
            self._evaluate_service(svc)

    def _evaluate_service(self, service_name: str) -> None:
        err = query_error_rate(self.prom, service_name)
        lat = query_latency_p95(self.prom, service_name)

        # Cold-start fallback: if Prom has no series yet, skip quietly
        # (load-test + chaos scripts will generate traffic and metrics).
        if err is not None:
            event = self.engine.evaluate_error_rate(service_name, err)
            if event:
                self._publish(event)
        if lat is not None:
            event = self.engine.evaluate_latency_ms(service_name, lat)
            if event:
                self._publish(event)

    def _publish(self, event: AnomalyEvent) -> None:
        enqueue(self.redis, settings.redis_queue_anomalies, event.to_redis_json())
        self.recent_anomalies.insert(0, event)
        self.recent_anomalies = self.recent_anomalies[:50]
        logger.warning(
            "anomaly published id=%s service=%s metric=%s value=%.4f severity=%s",
            event.id,
            event.service_name,
            event.metric_name,
            event.metric_value,
            event.severity.value,
        )

    def force_detect(
        self,
        service_name: str,
        metric_name: str,
        metric_value: float,
        threshold: float,
    ) -> AnomalyEvent:
        """Manual inject for demos / API — bypasses PromQL."""
        from aiops_shared.models import AnomalySeverity

        event = AnomalyEvent(
            service_name=service_name,
            metric_name=metric_name,
            metric_value=metric_value,
            threshold=threshold,
            severity=AnomalySeverity.HIGH,
            detector="manual",
            message=f"Manual anomaly inject: {metric_name}={metric_value}",
            labels={"service": service_name, "source": "api"},
        )
        self._publish(event)
        return event

    def status(self, *, deep: bool = False) -> dict:
        # /health must stay cheap — never block on PromQL during k8s/compose probes.
        out = {
            "poll_count": self.poll_count,
            "redis_ok": ping(self.redis),
            "recent_count": len(self.recent_anomalies),
            "last_error": self.last_error,
            "watched_services": list(WATCHED_SERVICES),
        }
        if deep:
            out["prometheus_ok"] = self.prom.healthy()
        return out
