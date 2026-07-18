"""
Prometheus HTTP API client — *pull* model.

Why query instead of remote_write?
----------------------------------
* Demo simplicity: no need to reconfigure every app's write path; LGTM already
  accepts OTLP and exposes a Prom-compatible query API on :9090.
* Isolation of blast radius: a buggy detector cannot corrupt TSDB write path.
* Easy local testing: curl the same PromQL the service uses.

Trade-off: pull latency = poll interval; not ideal for sub-second detection.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# PromQL templates — try demo-app custom metrics first, then OTel-style names,
# then classic Prometheus RED names (http_requests_total / duration_seconds).
ERROR_RATE_QUERIES = [
    # Mini demo apps (checkout/payment)
    (
        'sum(rate(demo_http_requests_total{{service_name="{svc}",status="error"}}[2m])) '
        '/ clamp_min(sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m])), 1e-9)'
    ),
    # Classic RED
    (
        'sum(rate(http_requests_total{{service="{svc}",status=~"5.."}}[2m])) '
        '/ clamp_min(sum(rate(http_requests_total{{service="{svc}"}}[2m])), 1e-9)'
    ),
    (
        'sum(rate(http_requests_total{{job="{svc}",code=~"5.."}}[2m])) '
        '/ clamp_min(sum(rate(http_requests_total{{job="{svc}"}}[2m])), 1e-9)'
    ),
    # OTel HTTP histogram (Astronomy Shop / LGTM)
    (
        'sum(rate(http_server_duration_milliseconds_count{{service_name="{svc}",'
        'http_status_code=~"5.."}}[2m])) '
        '/ clamp_min(sum(rate(http_server_duration_milliseconds_count'
        '{{service_name="{svc}"}}[2m])), 1e-9)'
    ),
    (
        'sum(rate(http_server_request_duration_seconds_count{{service_name="{svc}",'
        'http_status_code=~"5.."}}[2m])) '
        '/ clamp_min(sum(rate(http_server_request_duration_seconds_count'
        '{{service_name="{svc}"}}[2m])), 1e-9)'
    ),
    # gRPC services (payment, shipping, …) — non-OK status codes
    (
        'sum(rate(rpc_server_duration_milliseconds_count{{service_name="{svc}",'
        'rpc_grpc_status_code!="0"}}[2m])) '
        '/ clamp_min(sum(rate(rpc_server_duration_milliseconds_count'
        '{{service_name="{svc}"}}[2m])), 1e-9)'
    ),
    (
        'sum(rate(rpc_server_duration_seconds_count{{service_name="{svc}",'
        'rpc_grpc_status_code!="0"}}[2m])) '
        '/ clamp_min(sum(rate(rpc_server_duration_seconds_count'
        '{{service_name="{svc}"}}[2m])), 1e-9)'
    ),
]

REQUEST_RATE_QUERIES = [
    'sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m]))',
    'sum(rate(http_requests_total{{service="{svc}"}}[2m]))',
    'sum(rate(http_requests_total{{job="{svc}"}}[2m]))',
    'sum(rate(http_server_duration_milliseconds_count{{service_name="{svc}"}}[2m]))',
    'sum(rate(http_server_request_duration_seconds_count{{service_name="{svc}"}}[2m]))',
    'sum(rate(rpc_server_duration_milliseconds_count{{service_name="{svc}"}}[2m]))',
    'sum(rate(rpc_server_duration_seconds_count{{service_name="{svc}"}}[2m]))',
]

LATENCY_P95_QUERIES = [
    # Mini demo histogram (ms) → seconds
    'histogram_quantile(0.95, sum(rate(demo_http_duration_ms_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
    'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{service="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket{{job="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.95, sum(rate(http_server_duration_milliseconds_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
    'histogram_quantile(0.95, sum(rate(http_server_request_duration_seconds_bucket{{service_name="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.95, sum(rate(rpc_server_duration_milliseconds_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
    'histogram_quantile(0.95, sum(rate(rpc_server_duration_seconds_bucket{{service_name="{svc}"}}[2m])) by (le))',
]

# p99 — preferred narrative for explainability ("p99 latency cao hơn X sigma…")
LATENCY_P99_QUERIES = [
    'histogram_quantile(0.99, sum(rate(demo_http_duration_ms_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
    'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{job="{svc}"}}[2m])) by (le))',
    'histogram_quantile(0.99, sum(rate(http_server_duration_milliseconds_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
    'histogram_quantile(0.99, sum(rate(rpc_server_duration_milliseconds_bucket{{service_name="{svc}"}}[2m])) by (le)) / 1000',
]


class PrometheusClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or settings.prometheus_url).rstrip("/")
        self._client = httpx.Client(timeout=httpx.Timeout(2.5, connect=1.0))
        self._host_unreachable = False

    def close(self) -> None:
        self._client.close()

    def reset_unreachable(self) -> None:
        self._host_unreachable = False

    def query(self, promql: str) -> list[dict[str, Any]]:
        if self._host_unreachable:
            return []
        url = f"{self.base_url}/api/v1/query"
        try:
            resp = self._client.get(url, params={"query": promql})
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("status") != "success":
                logger.warning("prom non-success status=%s", payload.get("status"))
                return []
            self._host_unreachable = False
            return payload.get("data", {}).get("result", [])
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            self._host_unreachable = True
            logger.warning("prometheus unreachable url=%s err=%s", self.base_url, exc)
            return []
        except Exception as exc:
            logger.warning("prom query failed err=%s q=%s", exc, promql[:100])
            return []

    def first_scalar(self, templates: list[str], service: str) -> Optional[float]:
        for tmpl in templates:
            q = tmpl.format(svc=service)
            results = self.query(q)
            if not results:
                continue
            try:
                val = float(results[0]["value"][1])
                if val != val:  # NaN
                    continue
                return val
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return None

    def scrape_service(self, service: str) -> dict[str, Optional[float]]:
        """Return latest RED-ish features for one service (incl. p99 when available)."""
        return {
            "http_request_rate": self.first_scalar(REQUEST_RATE_QUERIES, service),
            "http_error_rate": self.first_scalar(ERROR_RATE_QUERIES, service),
            "http_latency_p95_seconds": self.first_scalar(LATENCY_P95_QUERIES, service),
            "http_latency_p99_seconds": self.first_scalar(LATENCY_P99_QUERIES, service),
        }

    def healthy(self) -> bool:
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
