"""Fraud scoring demo service — upstream dependency of payment."""

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

SERVICE_NAME = os.getenv("SERVICE_NAME", "fraud-service")
PORT = int(os.getenv("PORT", "8083"))

_state: dict[str, Any] = {
    "error_rate": float(os.getenv("ERROR_RATE", "0.01")),
    "base_latency_ms": float(os.getenv("BASE_LATENCY_MS", "30")),
    "extra_latency_ms": 0.0,
    # none | scoring_timeout | rule_engine_saturated | cpu_throttle
    "fault_mode": os.getenv("FAULT_MODE", "none"),
}

_FAULT_MESSAGES = {
    "scoring_timeout": "fraud-service scoring timeout / rule engine saturated",
    "rule_engine_saturated": "fraud-service scoring timeout / rule engine saturated",
    "cpu_throttle": "fraud-service worker/thread pool saturation or CPU throttle",
    "none": "fraud check failed",
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


app = FastAPI(title="Fraud Service", version="0.1.0", lifespan=lifespan)
setup_otel(SERVICE_NAME, app=app)


class ScoreRequest(BaseModel):
    order_id: str
    amount: float = Field(gt=0)
    currency: str = "USD"


class ChaosConfig(BaseModel):
    error_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    base_latency_ms: float | None = Field(default=None, ge=0)
    extra_latency_ms: float | None = Field(default=None, ge=0)
    fault_mode: str | None = Field(
        default=None,
        description="none|scoring_timeout|rule_engine_saturated|cpu_throttle",
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


@app.post("/score")
async def score(body: ScoreRequest) -> dict:
    start = time.perf_counter()
    attrs = {"service_name": SERVICE_NAME, "http_route": "/score"}

    delay_ms = _state["base_latency_ms"] + _state["extra_latency_ms"]
    delay_ms += random.uniform(0, delay_ms * 0.3)
    await asyncio.sleep(delay_ms / 1000.0)

    if random.random() < _state["error_rate"]:
        elapsed = (time.perf_counter() - start) * 1000
        req_counter.add(1, {**attrs, "status": "error"})
        err_counter.add(1, attrs)
        duration_hist.record(elapsed, {**attrs, "status": "error"})
        mode = str(_state.get("fault_mode") or "none")
        detail = _FAULT_MESSAGES.get(mode, _FAULT_MESSAGES["none"])
        logger.error(
            "fraud failure fault_mode=%s detail=%s order_id=%s amount=%s",
            mode,
            detail,
            body.order_id,
            body.amount,
        )
        raise HTTPException(status_code=503, detail=detail)

    elapsed = (time.perf_counter() - start) * 1000
    req_counter.add(1, {**attrs, "status": "ok"})
    duration_hist.record(elapsed, {**attrs, "status": "ok"})
    risk = round(min(0.9, max(0.05, body.amount / 1000.0)), 3)
    return {
        "order_id": body.order_id,
        "decision": "allow" if risk < 0.8 else "review",
        "risk_score": risk,
        "status": "scored",
        "latency_ms": round(elapsed, 2),
    }
