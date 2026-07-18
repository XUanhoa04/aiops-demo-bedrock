"""
Multi-Signal Context Gathering — run *alongside* algorithmic detection.

Pulls four families in one window so ConfidenceScorer and Decision Engine share
the same evidence snapshot:

  1. Metrics  (Prometheus / PromQL) — RED instant + short range
  2. Logs     (Loki / LogQL)        — error-ish lines + trace_id extraction
  3. Traces   (Tempo / TraceQL)     — error & slow traces for the service
  4. Events   (best-effort)         — chaos/deploy/change markers from logs
                                      or optional webhook-fed event list

Why gather at detect-time (not only in RCA)?
--------------------------------------------
RCA already re-gathers for LLM grounding. Detect-time gathering exists so that:
  * confidence can **gate** ticket creation before the human/RCA path
  * the Decision Engine sees completeness without a second hop
  * Prometheus gauges `context_completeness` reflect live pipeline health

Failures are non-fatal: we record sources_ok / gather_errors and still score
(with penalties) rather than drop the anomaly.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from app.config import settings
from app.confidence_scorer import compute_context_completeness
from app.models import SignalBundle

logger = logging.getLogger(__name__)

_TRACE_ID_RE = re.compile(
    r"(?:trace[_-]?id|traceId|tid)[=:\"'\s]+([0-9a-fA-F]{16,32})",
    re.IGNORECASE,
)
_HEX32_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")

# Patterns that look like change / chaos / operational events in logs
_EVENT_RE = re.compile(
    r"(?i)\b("
    r"deploy(?:ed|ment)?|rollback|restart|scale[d]?|"
    r"chaos|fault.?inject|kill|circuit.?break|"
    r"config.?change|feature.?flag|release|"
    r"maintenance|outage|incident"
    r")\b"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class ContextGatherer:
    """
    Concurrent multi-backend gatherer.

    Uses a small thread pool so Loki/Tempo/Prometheus latency does not stack
    serially on every anomaly (detect path budget is a few seconds max).
    """

    def __init__(
        self,
        prometheus_url: Optional[str] = None,
        loki_url: Optional[str] = None,
        tempo_url: Optional[str] = None,
        window_minutes: Optional[int] = None,
    ) -> None:
        self.prometheus_url = (prometheus_url or settings.prometheus_url).rstrip("/")
        self.loki_url = (loki_url or settings.loki_url).rstrip("/")
        self.tempo_url = (tempo_url or settings.tempo_url).rstrip("/")
        self.window_minutes = int(
            window_minutes if window_minutes is not None else settings.context_window_minutes
        )
        self._http = httpx.Client(timeout=httpx.Timeout(5.0, connect=1.5))

    def close(self) -> None:
        self._http.close()

    def probe(self) -> dict[str, bool]:
        return {
            "prometheus": self._ok(f"{self.prometheus_url}/api/v1/status/buildinfo"),
            "loki": self._ok(f"{self.loki_url}/ready")
            or self._ok(f"{self.loki_url}/loki/api/v1/labels"),
            "tempo": self._ok(f"{self.tempo_url}/ready")
            or self._ok(f"{self.tempo_url}/api/status/buildinfo"),
        }

    def _ok(self, url: str) -> bool:
        try:
            r = self._http.get(url, timeout=1.5)
            return r.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public gather
    # ------------------------------------------------------------------

    def gather(
        self,
        service: str,
        *,
        extra_features: Optional[dict[str, float]] = None,
        anchor: Optional[datetime] = None,
    ) -> SignalBundle:
        """
        Collect metrics / logs / traces / events for `service` around now
        (or `anchor`). `extra_features` seeds metrics when Prom is cold.
        """
        end = anchor or _utc_now()
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        start = end - timedelta(minutes=self.window_minutes)

        bundle = SignalBundle(
            window_start_iso=_iso(start),
            window_end_iso=_iso(end),
            sources_ok={},
            gather_errors=[],
        )

        # Concurrent fan-out — each worker catches its own exceptions
        tasks = {
            "metrics": lambda: self._gather_metrics(service, start, end, extra_features),
            "logs": lambda: self._gather_logs(service, start, end),
            "traces": lambda: self._gather_traces(service, start, end),
            "events": lambda: self._gather_events(service, start, end),
        }
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctx") as pool:
            futures = {pool.submit(fn): name for name, fn in tasks.items()}
            for fut in as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                    bundle.sources_ok[name] = True
                except Exception as exc:
                    results[name] = None
                    bundle.sources_ok[name] = False
                    bundle.gather_errors.append(f"{name}: {exc}")
                    logger.warning("context gather %s failed: %s", name, exc)

        # Map results → bundle fields
        if results.get("metrics") is not None:
            bundle.metrics = results["metrics"]
            bundle.sources_ok["prometheus"] = True
        else:
            bundle.sources_ok["prometheus"] = False
            if extra_features:
                # Degraded path: still expose detector features as metrics
                bundle.metrics = {
                    "service": service,
                    "instant": dict(extra_features),
                    "note": "prometheus_unavailable_using_detector_features",
                }

        if results.get("logs") is not None:
            bundle.logs = results["logs"]
            bundle.sources_ok["loki"] = True
        else:
            bundle.sources_ok["loki"] = False

        if results.get("traces") is not None:
            bundle.traces = results["traces"]
            bundle.sources_ok["tempo"] = True
        else:
            bundle.sources_ok["tempo"] = False

        if results.get("events") is not None:
            bundle.events = results["events"]
        else:
            bundle.events = []

        # Corroborate events from log scan if dedicated path was thin
        if not bundle.events and bundle.logs:
            bundle.events = _events_from_logs(bundle.logs)

        bundle.logs = [_enrich_log_trace_id(row) for row in bundle.logs]
        bundle.primary_trace_id = _select_primary_trace_id(bundle)
        bundle.completeness = compute_context_completeness(bundle)

        logger.info(
            "context service=%s prom=%s logs=%s traces=%s events=%s "
            "trace_id=%s completeness=%.2f missing=%s",
            service,
            bundle.sources_ok.get("prometheus"),
            len(bundle.logs),
            len(bundle.traces),
            len(bundle.events),
            bundle.primary_trace_id,
            bundle.completeness.ratio,
            bundle.completeness.missing,
        )
        return bundle

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _gather_metrics(
        self,
        service: str,
        start: datetime,
        end: datetime,
        extra_features: Optional[dict[str, float]],
    ) -> dict[str, Any]:
        error_rate_q = [
            (
                'sum(rate(demo_http_requests_total{{service_name="{svc}",status="error"}}[2m])) '
                '/ clamp_min(sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m])), 1e-9)'
            ),
            (
                'sum(rate(http_requests_total{{service="{svc}",status=~"5.."}}[2m])) '
                '/ clamp_min(sum(rate(http_requests_total{{service="{svc}"}}[2m])), 1e-9)'
            ),
        ]
        request_rate_q = [
            'sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m]))',
            'sum(rate(http_requests_total{{service="{svc}"}}[2m]))',
        ]
        latency_p95_q = [
            (
                "histogram_quantile(0.95, sum(rate(demo_http_duration_ms_bucket"
                '{{service_name="{svc}"}}[2m])) by (le)) / 1000'
            ),
            (
                "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket"
                '{{service="{svc}"}}[2m])) by (le))'
            ),
        ]
        # p99 — preferred narrative ("p99 latency cao hơn X sigma…")
        latency_p99_q = [
            (
                "histogram_quantile(0.99, sum(rate(demo_http_duration_ms_bucket"
                '{{service_name="{svc}"}}[2m])) by (le)) / 1000'
            ),
            (
                "histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket"
                '{{service="{svc}"}}[2m])) by (le))'
            ),
        ]

        instant = {
            "http_error_rate": self._first_scalar(error_rate_q, service),
            "http_request_rate": self._first_scalar(request_rate_q, service),
            "http_latency_p95_seconds": self._first_scalar(latency_p95_q, service),
            "http_latency_p99_seconds": self._first_scalar(latency_p99_q, service),
        }
        # Merge detector features if Prom missing some series
        if extra_features:
            for k, v in extra_features.items():
                if instant.get(k) is None and v is not None:
                    instant[k] = v

        return {
            "service": service,
            "window": {"start": _iso(start), "end": _iso(end)},
            "instant": instant,
            "range": {
                "http_error_rate": self._series_stats(
                    error_rate_q, service, start, end
                ),
                "http_request_rate": self._series_stats(
                    request_rate_q, service, start, end
                ),
                "http_latency_p95_seconds": self._series_stats(
                    latency_p95_q, service, start, end
                ),
                "http_latency_p99_seconds": self._series_stats(
                    latency_p99_q, service, start, end
                ),
            },
        }

    def _prom_query(self, promql: str) -> list[dict[str, Any]]:
        r = self._http.get(
            f"{self.prometheus_url}/api/v1/query",
            params={"query": promql},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prom status={body.get('status')}")
        return body.get("data", {}).get("result", []) or []

    def _prom_query_range(
        self, promql: str, start: datetime, end: datetime, step: str = "30s"
    ) -> list[dict[str, Any]]:
        r = self._http.get(
            f"{self.prometheus_url}/api/v1/query_range",
            params={
                "query": promql,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prom range status={body.get('status')}")
        return body.get("data", {}).get("result", []) or []

    def _first_scalar(
        self, templates: list[str], service: str
    ) -> Optional[float]:
        for tmpl in templates:
            q = tmpl.format(svc=service)
            try:
                results = self._prom_query(q)
            except Exception:
                continue
            if not results:
                continue
            try:
                val = float(results[0]["value"][1])
                if val != val:
                    continue
                return val
            except (KeyError, IndexError, TypeError, ValueError):
                continue
        return None

    def _series_stats(
        self, templates: list[str], service: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        for tmpl in templates:
            q = tmpl.format(svc=service)
            try:
                series = self._prom_query_range(q, start, end)
            except Exception:
                continue
            if not series:
                continue
            values: list[float] = []
            for s in series:
                for _ts, v in s.get("values") or []:
                    try:
                        f = float(v)
                        if f == f:
                            values.append(f)
                    except (TypeError, ValueError):
                        continue
            if not values:
                continue
            return {
                "promql": q,
                "points": len(values),
                "min": round(min(values), 6),
                "max": round(max(values), 6),
                "last": round(values[-1], 6),
                "avg": round(sum(values) / len(values), 6),
            }
        return {"promql": None, "points": 0, "note": "no series matched"}

    # ------------------------------------------------------------------
    # Loki
    # ------------------------------------------------------------------

    def _gather_logs(
        self, service: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        start_ns = int(start.timestamp() * 1e9)
        end_ns = int(end.timestamp() * 1e9)
        limit = settings.context_max_log_lines
        queries = [
            f'{{service_name="{service}"}} |~ "(?i)error|exception|traceback|failed|5[0-9]{{2}}"',
            f'{{service_name="{service}"}}',
            f'{{job=~".*{service}.*"}} |~ "(?i)error|exception|fail"',
            f'{{service="{service}"}} |~ "(?i)error|exception|fail"',
        ]
        for logql in queries:
            try:
                rows = self._loki_query_range(logql, start_ns, end_ns, limit=limit)
            except Exception as exc:
                logger.debug("logql failed q=%s err=%s", logql, exc)
                continue
            if rows:
                return rows
        return []

    def _loki_query_range(
        self, logql: str, start_ns: int, end_ns: int, limit: int
    ) -> list[dict[str, Any]]:
        r = self._http.get(
            f"{self.loki_url}/loki/api/v1/query_range",
            params={
                "query": logql,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": "backward",
            },
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"loki status={body.get('status')}")
        out: list[dict[str, Any]] = []
        for stream in (body.get("data") or {}).get("result") or []:
            labels = stream.get("stream") or {}
            for ts_ns, line in stream.get("values") or []:
                line_s = (line or "")[:500]
                out.append(
                    {
                        "ts_ns": ts_ns,
                        "line": line_s,
                        "trace_id": _extract_trace_id(line_s, labels),
                        "labels": {
                            k: labels[k]
                            for k in (
                                "service_name",
                                "service",
                                "job",
                                "level",
                                "trace_id",
                                "traceID",
                            )
                            if k in labels
                        },
                        "logql": logql,
                    }
                )
                if len(out) >= limit:
                    return out
        return out

    # ------------------------------------------------------------------
    # Tempo
    # ------------------------------------------------------------------

    def _gather_traces(
        self, service: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        limit = settings.context_max_traces
        min_start = int(start.timestamp())
        max_start = int(end.timestamp())
        candidates: list[tuple[str, dict[str, Any]]] = [
            (
                "traceql_error",
                {
                    "q": f'{{resource.service.name="{service}" && status=error}}',
                    "limit": limit,
                    "start": min_start,
                    "end": max_start,
                },
            ),
            (
                "traceql_slow",
                {
                    "q": f'{{resource.service.name="{service}" && duration>200ms}}',
                    "limit": limit,
                    "start": min_start,
                    "end": max_start,
                },
            ),
            (
                "traceql_any",
                {
                    "q": f'{{resource.service.name="{service}"}}',
                    "limit": limit,
                    "start": min_start,
                    "end": max_start,
                },
            ),
            (
                "tags",
                {
                    "tags": f'service.name="{service}"',
                    "limit": limit,
                    "minDuration": "100ms",
                    "start": min_start,
                    "end": max_start,
                },
            ),
        ]
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for mode, params in candidates:
            try:
                r = self._http.get(f"{self.tempo_url}/api/search", params=params)
                if r.status_code >= 400:
                    continue
                body = r.json()
            except Exception as exc:
                logger.debug("tempo search %s err=%s", mode, exc)
                continue
            for tr in body.get("traces") or []:
                tid = tr.get("traceID") or tr.get("traceId") or ""
                if not tid or tid in seen:
                    continue
                seen.add(tid)
                collected.append(
                    {
                        "trace_id": tid,
                        "root_service": tr.get("rootServiceName"),
                        "root_name": tr.get("rootTraceName"),
                        "duration_ms": tr.get("durationMs"),
                        "start_time_unix_nano": tr.get("startTimeUnixNano"),
                        "search_mode": mode,
                    }
                )
                if len(collected) >= limit:
                    return collected
        return collected

    # ------------------------------------------------------------------
    # Events (best-effort from Loki change/chaos markers)
    # ------------------------------------------------------------------

    def _gather_events(
        self, service: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """
        No dedicated event bus in this demo — derive change/chaos markers
        from Loki. Production would join deploy webhooks / K8s events / CMDB.
        """
        start_ns = int(start.timestamp() * 1e9)
        end_ns = int(end.timestamp() * 1e9)
        logql = (
            f'{{service_name="{service}"}} |~ '
            f'"(?i)deploy|chaos|restart|rollback|fault|scale|config.?change|release"'
        )
        try:
            rows = self._loki_query_range(logql, start_ns, end_ns, limit=15)
        except Exception:
            rows = []
        return _events_from_logs(rows)


def _events_from_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in logs:
        line = row.get("line") or ""
        m = _EVENT_RE.search(line)
        if not m:
            continue
        kind = m.group(1).lower()
        events.append(
            {
                "type": kind,
                "severity": "high"
                if kind in {"chaos", "rollback", "outage", "incident"}
                else "medium",
                "message": line[:240],
                "ts_ns": row.get("ts_ns"),
                "source": "loki_derived",
            }
        )
    return events[:15]


def _extract_trace_id(line: str, labels: Optional[dict] = None) -> Optional[str]:
    labels = labels or {}
    for k in ("trace_id", "traceID", "traceId"):
        if labels.get(k):
            return str(labels[k])
    m = _TRACE_ID_RE.search(line or "")
    if m:
        return m.group(1).lower()
    m2 = _HEX32_RE.search(line or "")
    if m2:
        return m2.group(1).lower()
    return None


def _enrich_log_trace_id(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("trace_id"):
        return row
    tid = _extract_trace_id(row.get("line") or "", row.get("labels") or {})
    if tid:
        row = dict(row)
        row["trace_id"] = tid
    return row


def _select_primary_trace_id(bundle: SignalBundle) -> Optional[str]:
    if bundle.traces:
        ranked = sorted(
            bundle.traces,
            key=lambda t: (t.get("duration_ms") or 0),
            reverse=True,
        )
        tid = ranked[0].get("trace_id")
        if tid:
            return str(tid)
    for row in bundle.logs:
        if row.get("trace_id"):
            return str(row["trace_id"])
    return None
