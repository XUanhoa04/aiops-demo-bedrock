"""
Prometheus HTTP API client (instant queries).

Points at LGTM's Prometheus-compatible endpoint (port 9090).
Demo apps export OTEL metrics; LGTM converts them into PromQL-queryable series.
When series are missing (cold start), we fall back to synthetic samples so the
pipeline remains demoable before scrapes populate.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class PrometheusClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.prometheus_url).rstrip("/")
        # Keep timeouts short: a cold LGTM or DNS blip must not exhaust the
        # default thread pool (uvicorn healthchecks share it with to_thread).
        self._client = httpx.Client(timeout=httpx.Timeout(2.0, connect=1.0))
        self._host_unreachable = False

    def close(self) -> None:
        self._client.close()

    def query(self, promql: str) -> list[dict[str, Any]]:
        if self._host_unreachable:
            return []
        url = f"{self.base_url}/api/v1/query"
        try:
            resp = self._client.get(url, params={"query": promql})
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                logger.warning("prom query non-success: %s", payload)
                return []
            self._host_unreachable = False
            return payload.get("data", {}).get("result", [])
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Fail fast for the rest of this poll cycle (and a few after).
            self._host_unreachable = True
            logger.warning("prom unreachable base=%s err=%s", self.base_url, exc)
            return []
        except Exception as exc:
            logger.warning("prom query failed q=%s err=%s", promql[:80], exc)
            return []

    def scalar(self, promql: str, default: float = 0.0) -> float:
        results = self.query(promql)
        if not results:
            return default
        try:
            # result[0].value = [timestamp, "string_value"]
            return float(results[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return default

    def healthy(self) -> bool:
        """Best-effort readiness; never blocks > ~1s (used only in /status)."""
        try:
            resp = self._client.get(
                f"{self.base_url}/api/v1/status/buildinfo",
                timeout=1.0,
            )
            ok = resp.status_code == 200
            if ok:
                self._host_unreachable = False
            return ok
        except Exception:
            return False

    def reset_unreachable(self) -> None:
        """Called at the start of each poll so we re-probe after LGTM boots."""
        self._host_unreachable = False


# PromQL helpers — OTEL HTTP metrics often land as:
#   http_server_duration_milliseconds_bucket / http_server_request_duration_*
# Naming varies by SDK version; we try multiple expressions and use the first hit.

ERROR_RATE_QUERIES = [
    # Prefer explicit counters if demo apps register them
    'sum(rate(demo_http_requests_total{{service_name="{svc}",status="error"}}[2m])) '
    '/ clamp_min(sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m])), 1e-9)',
    'sum(rate(http_server_duration_milliseconds_count{{service_name="{svc}",'
    'http_status_code=~"5.."}}[2m])) '
    '/ clamp_min(sum(rate(http_server_duration_milliseconds_count{{service_name="{svc}"}}[2m])), 1e-9)',
]

LATENCY_P95_QUERIES = [
    'histogram_quantile(0.95, sum(rate(demo_http_duration_ms_bucket{{service_name="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.95, sum(rate(http_server_duration_milliseconds_bucket{{service_name="{svc}"}}[2m])) by (le))',
]


def query_error_rate(prom: PrometheusClient, service_name: str) -> Optional[float]:
    for tmpl in ERROR_RATE_QUERIES:
        q = tmpl.format(svc=service_name)
        results = prom.query(q)
        if results:
            try:
                return float(results[0]["value"][1])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
    return None


def query_latency_p95(prom: PrometheusClient, service_name: str) -> Optional[float]:
    for tmpl in LATENCY_P95_QUERIES:
        q = tmpl.format(svc=service_name)
        results = prom.query(q)
        if results:
            try:
                val = float(results[0]["value"][1])
                # NaN from empty histograms
                if val != val:  # noqa: PLR0124 — NaN check
                    continue
                return val
            except (KeyError, IndexError, ValueError, TypeError):
                continue
    return None
