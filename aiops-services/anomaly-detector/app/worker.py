"""
Background poll loop:

  Prometheus → hybrid score → multi-signal context → confidence
            → Prometheus gauges → Decision object → notify
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from aiops_shared.models import AnomalyEvent

from app.config import settings
from app.decision_builder import DecisionBuilder
from app.detector import HybridDetector, HybridResult
from app.models import DetectionDecision
from app.notifier import Notifier
from app.prom_metrics import (
    ERRORS_TOTAL,
    POLLS_TOTAL,
    set_detection_methods,
    set_score,
)
from app.prometheus_client import PrometheusClient

logger = logging.getLogger(__name__)


class DetectorWorker:
    def __init__(self) -> None:
        self.engine = HybridDetector()
        self.prom = PrometheusClient()
        self.decisions = DecisionBuilder()
        self.notifier = Notifier()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.recent_anomalies: list[AnomalyEvent] = []
        self.recent_results: list[dict] = []
        self.recent_decisions: list[DetectionDecision] = []
        self.poll_count = 0
        self.last_error: Optional[str] = None
        self._last_fired: dict[str, float] = {}

    async def start(self) -> None:
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="hybrid-anomaly-poller")
        logger.info(
            "hybrid detector started interval=%ss zscore_threshold=%.2f "
            "ewma_alpha=%.2f vote=%s stl=%s context=%s services=%s "
            "confidence_weights=m%.0f/t%.0f/l%.0f/e%.0f",
            settings.poll_interval_sec,
            settings.zscore_threshold,
            settings.ewma_alpha,
            settings.hybrid_vote,
            settings.enable_stl,
            settings.enable_context_gather,
            settings.watched_service_list(),
            settings.confidence_weight_metrics * 100,
            settings.confidence_weight_traces * 100,
            settings.confidence_weight_logs * 100,
            settings.confidence_weight_events * 100,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=15)
        self.prom.close()
        self.decisions.close()
        self.notifier.close()
        logger.info("hybrid detector stopped polls=%s", self.poll_count)

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self._poll_once)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                ERRORS_TOTAL.labels(stage="poll").inc()
                logger.exception("poll cycle failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=settings.poll_interval_sec,
                )
            except asyncio.TimeoutError:
                pass

    def _poll_once(self) -> None:
        self.poll_count += 1
        POLLS_TOTAL.inc()
        self.prom.reset_unreachable()
        for svc in settings.watched_service_list():
            try:
                self._evaluate_service(svc)
            except Exception as exc:
                ERRORS_TOTAL.labels(stage="evaluate").inc()
                logger.exception("evaluate failed service=%s err=%s", svc, exc)

    def _evaluate_service(self, service: str) -> None:
        features = self.prom.scrape_service(service)
        present = {k: v for k, v in features.items() if v is not None}
        if not present:
            logger.debug("no metrics yet for service=%s (cold start)", service)
            return

        logger.info(
            "scraped service=%s features=%s",
            service,
            {k: round(v, 5) if v is not None else None for k, v in features.items()},
        )

        results = self.engine.evaluate_service(service, features)
        for result in results:
            self._export_prom_metrics(result)
            # Build decision (context + confidence) for every evaluation so
            # gauges stay fresh; only anomalies notify downstream.
            decision = self.decisions.build(result)
            self._store_result(result, decision)

            if result.is_anomaly:
                self._maybe_notify(result, decision)

    def _store_result(self, result: HybridResult, decision: DetectionDecision) -> None:
        self.recent_results.insert(
            0,
            {
                "service": result.service,
                "metric": result.metric,
                "value": result.value,
                "is_anomaly": result.is_anomaly,
                "anomaly_score": result.anomaly_score,
                "winning_methods": result.winning_methods,
                "explanation": decision.explanation,
                "confidence_score": decision.confidence_score,
                "context_completeness": decision.context_completeness,
                "missing_context": decision.missing_context,
                "methods": [
                    {
                        "method": m.method,
                        "score": m.score,
                        "is_anomaly": m.is_anomaly,
                        "explanation": m.explanation,
                    }
                    for m in result.methods
                ],
            },
        )
        self.recent_results = self.recent_results[:100]
        self.recent_decisions.insert(0, decision)
        self.recent_decisions = self.recent_decisions[:50]

    def _export_prom_metrics(self, result: HybridResult) -> None:
        method_flags: dict[str, bool] = {}
        for m in result.methods:
            set_score(
                result.service,
                result.metric,
                m.method,
                m.score,
                m.is_anomaly,
            )
            method_flags[m.method] = m.method in result.winning_methods
        # Hybrid aggregate series
        set_score(
            result.service,
            result.metric,
            "hybrid",
            result.anomaly_score,
            result.is_anomaly,
        )
        method_flags["hybrid"] = result.is_anomaly
        set_detection_methods(result.service, result.metric, method_flags)

    def _maybe_notify(
        self, result: HybridResult, decision: DetectionDecision
    ) -> None:
        key = f"{result.service}:{result.metric}"
        now = time.time()
        last = self._last_fired.get(key, 0.0)
        if now - last < settings.alert_cooldown_sec:
            logger.debug("cooldown active key=%s", key)
            return
        self._last_fired[key] = now
        event = self.decisions.to_anomaly_event(decision)
        event = self.notifier.publish_decision(decision, event)
        self.recent_anomalies.insert(0, event)
        self.recent_anomalies = self.recent_anomalies[:50]

    def force_detect(
        self,
        service_name: str,
        metric_name: str,
        metric_value: float,
        threshold: float,
        *,
        gather_context: bool = True,
    ) -> tuple[AnomalyEvent, DetectionDecision]:
        result = self.engine.force_score(
            service_name, metric_name, metric_value, threshold
        )
        self._export_prom_metrics(result)
        decision = self.decisions.build(result, gather_context=gather_context)
        self._store_result(result, decision)
        # Manual path always notifies (demo reliability); bypass cooldown
        # but still respect min_confidence_to_notify inside notifier.
        event = self.decisions.to_anomaly_event(decision)
        event = self.notifier.publish_decision(decision, event)
        self.recent_anomalies.insert(0, event)
        self.recent_anomalies = self.recent_anomalies[:50]
        return event, decision

    def status(self, *, deep: bool = False) -> dict:
        out = {
            "poll_count": self.poll_count,
            "redis_ok": self.notifier.redis_ok(),
            "recent_count": len(self.recent_anomalies),
            "recent_decisions": len(self.recent_decisions),
            "last_error": self.last_error,
            "watched_services": settings.watched_service_list(),
            "poll_interval_sec": settings.poll_interval_sec,
            "zscore_threshold": settings.zscore_threshold,
            "ewma_alpha": settings.ewma_alpha,
            "hybrid_vote": settings.hybrid_vote,
            "window_size": settings.window_size,
            "enable_stl": settings.enable_stl,
            "enable_context_gather": settings.enable_context_gather,
            "confidence_weights": {
                "metrics": settings.confidence_weight_metrics,
                "traces": settings.confidence_weight_traces,
                "logs": settings.confidence_weight_logs,
                "events": settings.confidence_weight_events,
            },
            "notify": {
                "redis": settings.enable_redis_notify,
                "webhook": settings.enable_webhook_notify,
                "webhook_url": settings.incident_webhook_url or None,
                "min_confidence_to_notify": settings.min_confidence_to_notify,
            },
        }
        if deep:
            out["prometheus_ok"] = self.prom.healthy()
            try:
                out["context_backends"] = self.decisions.gatherer.probe()
            except Exception as exc:
                out["context_backends"] = {"error": str(exc)}
            out["latest_results"] = self.recent_results[:5]
            out["latest_decisions"] = [
                d.to_decision_dict() for d in self.recent_decisions[:3]
            ]
        return out
