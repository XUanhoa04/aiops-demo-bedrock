"""
Anomaly detection algorithms.

Production choices:
- Threshold rules for CV-demo clarity (explainable in interviews).
- Lightweight z-score on a sliding window as a second signal.
- Real platforms: Prometheus recording rules + ML (Prophet, IsolationForest),
  or managed detectors (CloudWatch Anomaly Detection, Datadog Watchdog).
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict, deque
from typing import Deque, Iterable, Optional

from aiops_shared.models import AnomalyEvent, AnomalySeverity

from app.config import settings

logger = logging.getLogger(__name__)


class SlidingWindow:
    def __init__(self, maxlen: int) -> None:
        self._values: Deque[float] = deque(maxlen=maxlen)

    def add(self, value: float) -> None:
        self._values.append(value)

    def zscore(self, value: float) -> Optional[float]:
        if len(self._values) < 5:
            return None
        mean = statistics.fmean(self._values)
        # population stdev; avoid division by zero on flat series
        var = statistics.pvariance(self._values)
        std = math.sqrt(var) if var > 0 else 0.0
        if std < 1e-9:
            return 0.0 if abs(value - mean) < 1e-9 else float("inf")
        return (value - mean) / std


class AnomalyEngine:
    def __init__(self) -> None:
        self._windows: dict[str, SlidingWindow] = defaultdict(
            lambda: SlidingWindow(settings.zscore_window)
        )
        # Dedup: avoid flooding the queue with the same anomaly every poll
        self._last_fired: dict[str, float] = {}
        self._cooldown_sec = 60.0

    def evaluate_error_rate(
        self,
        service_name: str,
        error_rate: float,
        labels: Optional[dict[str, str]] = None,
    ) -> Optional[AnomalyEvent]:
        key = f"{service_name}:error_rate"
        self._windows[key].add(error_rate)

        threshold = settings.anomaly_error_rate_threshold
        severity = self._severity_for_ratio(error_rate, threshold)
        events: list[AnomalyEvent] = []

        if error_rate >= threshold:
            events.append(
                AnomalyEvent(
                    service_name=service_name,
                    metric_name="http_error_rate",
                    metric_value=error_rate,
                    threshold=threshold,
                    severity=severity,
                    detector="threshold",
                    message=(
                        f"Error rate {error_rate:.2%} exceeds threshold "
                        f"{threshold:.2%} for {service_name}"
                    ),
                    labels=labels or {"service": service_name},
                    context={"unit": "ratio"},
                )
            )

        z = self._windows[key].zscore(error_rate)
        if z is not None and z >= settings.zscore_sigma and error_rate > 0.05:
            events.append(
                AnomalyEvent(
                    service_name=service_name,
                    metric_name="http_error_rate",
                    metric_value=error_rate,
                    threshold=float(settings.zscore_sigma),
                    severity=AnomalySeverity.HIGH if z >= 4 else AnomalySeverity.MEDIUM,
                    detector="zscore",
                    message=(
                        f"Error rate z-score={z:.2f} (sigma≥{settings.zscore_sigma}) "
                        f"for {service_name}"
                    ),
                    labels=labels or {"service": service_name},
                    context={"zscore": z, "unit": "ratio"},
                )
            )

        return self._pick_and_dedup(key, events)

    def evaluate_latency_ms(
        self,
        service_name: str,
        latency_p95_ms: float,
        labels: Optional[dict[str, str]] = None,
    ) -> Optional[AnomalyEvent]:
        key = f"{service_name}:latency_p95"
        self._windows[key].add(latency_p95_ms)
        threshold = settings.anomaly_latency_p95_ms

        if latency_p95_ms < threshold:
            # still feed the window for z-score baseline
            z = self._windows[key].zscore(latency_p95_ms)
            if z is None or z < settings.zscore_sigma:
                return None

        severity = (
            AnomalySeverity.CRITICAL
            if latency_p95_ms >= threshold * 2
            else AnomalySeverity.HIGH
            if latency_p95_ms >= threshold
            else AnomalySeverity.MEDIUM
        )
        event = AnomalyEvent(
            service_name=service_name,
            metric_name="http_latency_p95_ms",
            metric_value=latency_p95_ms,
            threshold=threshold,
            severity=severity,
            detector="threshold",
            message=(
                f"p95 latency {latency_p95_ms:.0f}ms exceeds "
                f"{threshold:.0f}ms for {service_name}"
            ),
            labels=labels or {"service": service_name},
            context={"unit": "ms"},
        )
        return self._pick_and_dedup(key, [event])

    def _pick_and_dedup(
        self, key: str, events: Iterable[AnomalyEvent]
    ) -> Optional[AnomalyEvent]:
        import time

        events = list(events)
        if not events:
            return None
        # Prefer highest severity
        rank = {
            AnomalySeverity.LOW: 1,
            AnomalySeverity.MEDIUM: 2,
            AnomalySeverity.HIGH: 3,
            AnomalySeverity.CRITICAL: 4,
        }
        best = max(events, key=lambda e: rank[e.severity])
        now = time.time()
        last = self._last_fired.get(key, 0.0)
        if now - last < self._cooldown_sec:
            logger.debug("cooldown active key=%s", key)
            return None
        self._last_fired[key] = now
        return best

    @staticmethod
    def _severity_for_ratio(value: float, threshold: float) -> AnomalySeverity:
        if value >= threshold * 3:
            return AnomalySeverity.CRITICAL
        if value >= threshold * 2:
            return AnomalySeverity.HIGH
        if value >= threshold:
            return AnomalySeverity.MEDIUM
        return AnomalySeverity.LOW
