"""
OpenTelemetry bootstrap for FastAPI services.

Production choices:
- OTLP HTTP (4318) is the compose default — simpler than gRPC for demos.
- We instrument FastAPI + requests/httpx + logging when available.
- Fail-open: if the collector is down, the app still starts (critical for demos).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def setup_otel(
    service_name: Optional[str] = None,
    app=None,
) -> None:
    """
    Initialize TracerProvider + MeterProvider exporting to OTEL_EXPORTER_OTLP_ENDPOINT.

    Call once at process startup, before serving traffic.
    """
    service_name = (
        service_name
        or os.getenv("OTEL_SERVICE_NAME")
        or os.getenv("SERVICE_NAME")
        or "unknown-service"
    )
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://lgtm:4318")

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OpenTelemetry packages not installed — running without telemetry "
            "(ok for unit tests, not for the compose demo)."
        )
        return

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "aiops-demo",
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "demo"),
        }
    )

    # --- Traces ---
    # BatchSpanProcessor is production-default (async export, backpressure).
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    export_interval_ms = int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "5000"))
    metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint.rstrip('/')}/v1/metrics")
    reader = PeriodicExportingMetricReader(
        metric_exporter,
        export_interval_millis=export_interval_ms,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # --- Auto-instrument FastAPI if app provided ---
    # Must run BEFORE the app starts serving (middleware registration).
    # Prefer calling setup_otel(...) at module import after `app = FastAPI(...)`,
    # or use lifespan only for non-middleware init.
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            # instrument_app fails if middleware stack is already frozen
            if not getattr(app.state, "_otel_instrumented", False):
                FastAPIInstrumentor.instrument_app(app)
                app.state._otel_instrumented = True
        except Exception as exc:  # pragma: no cover
            logger.warning("FastAPI instrumentation skipped: %s", exc)

    try:
        from opentelemetry.instrumentation.logging import LoggingInstrumentor

        LoggingInstrumentor().instrument(set_logging_format=True)
    except Exception:
        pass

    logger.info(
        "OTEL initialized service=%s endpoint=%s",
        service_name,
        endpoint,
    )
