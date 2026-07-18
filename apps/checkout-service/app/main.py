"""
Checkout demo service.

Production choices for the demo app tier:
- Emits custom RED metrics (rate/errors/duration) that anomaly-detector can query.
- Chaos knobs via env + runtime API so scripts can inject failures without rebuilds.
- Calls payment-service to create a small distributed trace for Tempo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

setup_logging()
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "checkout-service")
PORT = int(os.getenv("PORT", "8080"))
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8081")

# Mutable runtime chaos state (also seeded from env).
# fault_mode adds production-like error messages for logs/traces evaluation demos.
_state: dict[str, Any] = {
    "error_rate": float(os.getenv("ERROR_RATE", "0.02")),
    "base_latency_ms": float(os.getenv("BASE_LATENCY_MS", "50")),
    "extra_latency_ms": 0.0,
    # none | db_pool | cache_miss | dependency_timeout | cpu_throttle
    "fault_mode": os.getenv("FAULT_MODE", "none"),
}

_FAULT_MESSAGES = {
    "db_pool": "database connection pool exhausted (no free connections)",
    "cache_miss": "cache miss storm — cold redis keyspace, fallback to origin",
    "dependency_timeout": "upstream dependency timeout (payment gateway)",
    "cpu_throttle": "worker thread pool saturated / CPU throttle",
    "none": "checkout artificially failed",
}


def _get_meter():
    try:
        from opentelemetry import metrics

        return metrics.get_meter(SERVICE_NAME)
    except Exception:
        return None


class _Noop:
    def add(self, *a, **k): ...
    def record(self, *a, **k): ...


meter = None
req_counter = _Noop()
err_counter = _Noop()
duration_hist = _Noop()


def _init_metrics() -> None:
    global meter, req_counter, err_counter, duration_hist
    meter = _get_meter()
    if meter is None:
        return
    req_counter = meter.create_counter(
        "demo_http_requests_total",
        description="Total checkout HTTP requests",
        unit="1",
    )
    err_counter = meter.create_counter(
        "demo_http_errors_total",
        description="Checkout errors",
        unit="1",
    )
    # Explicit histogram for PromQL histogram_quantile demos
    duration_hist = meter.create_histogram(
        "demo_http_duration_ms",
        description="Checkout request duration",
        unit="ms",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_metrics()
    yield


app = FastAPI(title="Checkout Service", version="0.1.0", lifespan=lifespan)
setup_otel(SERVICE_NAME, app=app)


class CheckoutRequest(BaseModel):
    order_id: str = Field(default="ord-demo-1")
    amount: float = Field(default=42.0, gt=0)
    currency: str = "USD"


class ChaosConfig(BaseModel):
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    base_latency_ms: float | None = Field(default=None, ge=0)
    extra_latency_ms: float | None = Field(default=None, ge=0)
    fault_mode: str | None = Field(
        default=None,
        description="none|db_pool|cache_miss|dependency_timeout|cpu_throttle",
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        details={"chaos": _state},
    )


@app.get("/chaos")
def get_chaos() -> dict:
    return _state


@app.post("/chaos")
def set_chaos(cfg: ChaosConfig) -> dict:
    """Runtime chaos injection — used by scripts/chaos.py and dynamic_load stages."""
    if cfg.error_rate is not None:
        _state["error_rate"] = cfg.error_rate
    if cfg.base_latency_ms is not None:
        _state["base_latency_ms"] = cfg.base_latency_ms
    if cfg.extra_latency_ms is not None:
        _state["extra_latency_ms"] = cfg.extra_latency_ms
    if cfg.fault_mode is not None:
        _state["fault_mode"] = cfg.fault_mode
    logger.warning(
        "chaos updated service=%s fault_mode=%s error_rate=%s extra_latency_ms=%s",
        SERVICE_NAME,
        _state.get("fault_mode"),
        _state.get("error_rate"),
        _state.get("extra_latency_ms"),
    )
    return _state


@app.post("/checkout")
async def checkout(body: CheckoutRequest) -> dict:
    start = time.perf_counter()
    attrs = {
        "service_name": SERVICE_NAME,
        "http_route": "/checkout",
    }

    # Artificial latency (base + chaos)
    delay_ms = _state["base_latency_ms"] + _state["extra_latency_ms"]
    delay_ms += random.uniform(0, delay_ms * 0.2)
    await asyncio.sleep(delay_ms / 1000.0)

    # Local error injection (message depends on fault_mode for realistic Loki lines)
    if random.random() < _state["error_rate"]:
        elapsed = (time.perf_counter() - start) * 1000
        req_counter.add(1, {**attrs, "status": "error"})
        err_counter.add(1, attrs)
        duration_hist.record(elapsed, {**attrs, "status": "error"})
        mode = str(_state.get("fault_mode") or "none")
        detail = _FAULT_MESSAGES.get(mode, _FAULT_MESSAGES["none"])
        logger.error(
            "checkout failure fault_mode=%s detail=%s order_id=%s",
            mode,
            detail,
            body.order_id,
        )
        raise HTTPException(status_code=503, detail=detail)

    # Downstream payment call (distributed trace)
    payment_status = "skipped"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{PAYMENT_URL.rstrip('/')}/pay",
                json={
                    "order_id": body.order_id,
                    "amount": body.amount,
                    "currency": body.currency,
                },
            )
            payment_status = "ok" if resp.is_success else "failed"
            if not resp.is_success:
                elapsed = (time.perf_counter() - start) * 1000
                req_counter.add(1, {**attrs, "status": "error"})
                err_counter.add(1, {**attrs, "reason": "payment"})
                duration_hist.record(elapsed, {**attrs, "status": "error"})
                raise HTTPException(
                    status_code=502,
                    detail=f"payment failed: {resp.status_code}",
                )
            payment_body = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        elapsed = (time.perf_counter() - start) * 1000
        req_counter.add(1, {**attrs, "status": "error"})
        err_counter.add(1, {**attrs, "reason": "payment_transport"})
        duration_hist.record(elapsed, {**attrs, "status": "error"})
        raise HTTPException(status_code=502, detail=f"payment error: {exc}") from exc

    elapsed = (time.perf_counter() - start) * 1000
    req_counter.add(1, {**attrs, "status": "ok"})
    duration_hist.record(elapsed, {**attrs, "status": "ok"})
    return {
        "order_id": body.order_id,
        "status": "confirmed",
        "payment": payment_body,
        "latency_ms": round(elapsed, 2),
        "payment_status": payment_status,
    }
