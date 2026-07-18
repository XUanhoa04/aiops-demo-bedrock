"""Payment demo service — downstream of checkout, chaos-controllable."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

setup_logging()
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "payment-service")
PORT = int(os.getenv("PORT", "8081"))

_state: dict[str, Any] = {
    "error_rate": float(os.getenv("ERROR_RATE", "0.01")),
    "base_latency_ms": float(os.getenv("BASE_LATENCY_MS", "80")),
    "extra_latency_ms": 0.0,
}


class _Noop:
    def add(self, *a, **k): ...
    def record(self, *a, **k): ...


req_counter = _Noop()
err_counter = _Noop()
duration_hist = _Noop()


def _init_metrics() -> None:
    global req_counter, err_counter, duration_hist
    try:
        from opentelemetry import metrics

        meter = metrics.get_meter(SERVICE_NAME)
        req_counter = meter.create_counter("demo_http_requests_total", unit="1")
        err_counter = meter.create_counter("demo_http_errors_total", unit="1")
        duration_hist = meter.create_histogram("demo_http_duration_ms", unit="ms")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_metrics()
    yield


app = FastAPI(title="Payment Service", version="0.1.0", lifespan=lifespan)
setup_otel(SERVICE_NAME, app=app)


class PayRequest(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    currency: str = "USD"


class ChaosConfig(BaseModel):
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    base_latency_ms: float | None = Field(default=None, ge=0)
    extra_latency_ms: float | None = Field(default=None, ge=0)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME, details={"chaos": _state})


@app.get("/chaos")
def get_chaos() -> dict:
    return _state


@app.post("/chaos")
def set_chaos(cfg: ChaosConfig) -> dict:
    if cfg.error_rate is not None:
        _state["error_rate"] = cfg.error_rate
    if cfg.base_latency_ms is not None:
        _state["base_latency_ms"] = cfg.base_latency_ms
    if cfg.extra_latency_ms is not None:
        _state["extra_latency_ms"] = cfg.extra_latency_ms
    logger.warning("chaos updated %s", _state)
    return _state


@app.post("/pay")
async def pay(body: PayRequest) -> dict:
    start = time.perf_counter()
    attrs = {"service_name": SERVICE_NAME, "http_route": "/pay"}

    delay_ms = _state["base_latency_ms"] + _state["extra_latency_ms"]
    delay_ms += random.uniform(0, delay_ms * 0.25)
    await asyncio.sleep(delay_ms / 1000.0)

    if random.random() < _state["error_rate"]:
        elapsed = (time.perf_counter() - start) * 1000
        req_counter.add(1, {**attrs, "status": "error"})
        err_counter.add(1, attrs)
        duration_hist.record(elapsed, {**attrs, "status": "error"})
        raise HTTPException(status_code=502, detail="payment gateway timeout (injected)")

    elapsed = (time.perf_counter() - start) * 1000
    req_counter.add(1, {**attrs, "status": "ok"})
    duration_hist.record(elapsed, {**attrs, "status": "ok"})
    return {
        "payment_id": str(uuid4()),
        "order_id": body.order_id,
        "amount": body.amount,
        "currency": body.currency,
        "status": "captured",
        "latency_ms": round(elapsed, 2),
    }
