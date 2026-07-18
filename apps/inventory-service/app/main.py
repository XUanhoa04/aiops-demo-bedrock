"""Inventory demo service — stock reserve dependency of checkout."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from aiops_shared.logging_config import setup_logging
from aiops_shared.models import HealthResponse
from aiops_shared.otel import setup_otel

setup_logging()
logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "inventory-service")
PORT = int(os.getenv("PORT", "8082"))

_state: dict[str, Any] = {
    "error_rate": float(os.getenv("ERROR_RATE", "0.01")),
    "base_latency_ms": float(os.getenv("BASE_LATENCY_MS", "40")),
    "extra_latency_ms": 0.0,
    # none | stock_lock | db_pool | cache_miss
    "fault_mode": os.getenv("FAULT_MODE", "none"),
}

_FAULT_MESSAGES = {
    "stock_lock": "inventory stock lock wait timeout sku locks exhausted",
    "db_pool": "inventory-service database connection pool exhaustion",
    "cache_miss": "inventory redis cache miss — cold stock keyspace",
    "none": "inventory reserve failed",
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


app = FastAPI(title="Inventory Service", version="0.1.0", lifespan=lifespan)
setup_otel(SERVICE_NAME, app=app)


class ReserveRequest(BaseModel):
    order_id: str
    sku: str = "SKU-DEMO"
    qty: int = Field(default=1, ge=1)


class ChaosConfig(BaseModel):
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    base_latency_ms: float | None = Field(default=None, ge=0)
    extra_latency_ms: float | None = Field(default=None, ge=0)
    fault_mode: str | None = Field(
        default=None, description="none|stock_lock|db_pool|cache_miss"
    )


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


@app.post("/reserve")
async def reserve(body: ReserveRequest) -> dict:
    start = time.perf_counter()
    attrs = {"service_name": SERVICE_NAME, "http_route": "/reserve"}

    delay_ms = _state["base_latency_ms"] + _state["extra_latency_ms"]
    delay_ms += random.uniform(0, delay_ms * 0.25)
    await asyncio.sleep(delay_ms / 1000.0)

    if random.random() < _state["error_rate"]:
        elapsed = (time.perf_counter() - start) * 1000
        req_counter.add(1, {**attrs, "status": "error"})
        err_counter.add(1, attrs)
        duration_hist.record(elapsed, {**attrs, "status": "error"})
        mode = str(_state.get("fault_mode") or "none")
        detail = _FAULT_MESSAGES.get(mode, _FAULT_MESSAGES["none"])
        logger.error(
            "inventory failure fault_mode=%s detail=%s order_id=%s sku=%s",
            mode,
            detail,
            body.order_id,
            body.sku,
        )
        raise HTTPException(status_code=503, detail=detail)

    elapsed = (time.perf_counter() - start) * 1000
    req_counter.add(1, {**attrs, "status": "ok"})
    duration_hist.record(elapsed, {**attrs, "status": "ok"})
    return {
        "reservation_id": f"res-{body.order_id}",
        "order_id": body.order_id,
        "sku": body.sku,
        "qty": body.qty,
        "status": "reserved",
        "latency_ms": round(elapsed, 2),
    }
