"""
Grounded context gathering from the LGTM stack.

Production note — why ground the LLM?
-------------------------------------
Un-grounded RCA (model free-associating from the ticket title alone) invents
plausible-but-false causes: wrong services, phantom deploys, fictional error
codes. Ops needs *evidence-bound* reasoning:

1. Pull only observables the on-call could verify in Grafana (Prom/Loki/Tempo).
2. Put those facts in the prompt as the *only* allowed evidence set.
3. Require structured JSON so downstream systems can act without NLP.

If a backend is down we record `gather_errors` and let the model (or rule
fallback) say "insufficient evidence" rather than fabricate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from aiops_shared.topology import (
    infer_edges_from_traces,
    load_topology_catalog,
)

from app.config import settings
from app.models import EvidencePack

logger = logging.getLogger(__name__)

_CHANGE_RE = re.compile(
    r"(?i)\b(deploy|rollback|restart|chaos|fault.?inject|scale|release|config.?change)\b"
)

# Common OTel / app log patterns for correlating logs → Tempo
_TRACE_ID_RE = re.compile(
    r"(?:trace[_-]?id|traceId|tid)[=:\"'\s]+([0-9a-fA-F]{16,32})",
    re.IGNORECASE,
)
_HEX32_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


class EvidenceGatherer:
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
        self.window_minutes = int(window_minutes or settings.evidence_window_minutes)
        self._http = httpx.Client(timeout=httpx.Timeout(8.0, connect=2.0))

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------
    # Health probes (for /health)
    # ------------------------------------------------------------------

    def probe(self) -> dict[str, bool]:
        return {
            "prometheus": self._ok(f"{self.prometheus_url}/api/v1/status/buildinfo"),
            "loki": self._ok(f"{self.loki_url}/ready") or self._ok(f"{self.loki_url}/loki/api/v1/labels"),
            "tempo": self._ok(f"{self.tempo_url}/ready") or self._ok(f"{self.tempo_url}/api/status/buildinfo"),
        }

    def _ok(self, url: str) -> bool:
        try:
            r = self._http.get(url, timeout=2.0)
            return r.status_code < 500
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Main gather
    # ------------------------------------------------------------------

    def gather(self, incident: dict[str, Any]) -> EvidencePack:
        service = (
            incident.get("service_name")
            or (incident.get("labels") or {}).get("service")
            or "unknown"
        )
        end = _utc_now()
        # Prefer incident created_at as anchor when present
        created = incident.get("created_at")
        if created:
            try:
                if isinstance(created, str):
                    anchor = datetime.fromisoformat(created.replace("Z", "+00:00"))
                else:
                    anchor = end
                if anchor.tzinfo is None:
                    anchor = anchor.replace(tzinfo=timezone.utc)
                # Window centered on / trailing the incident
                end = max(end, anchor + timedelta(minutes=2))
            except Exception:
                pass
        start = end - timedelta(minutes=self.window_minutes)

        pack = EvidencePack(
            incident_id=str(incident.get("id") or ""),
            service_name=service,
            window_minutes=self.window_minutes,
            window_start_iso=_iso(start),
            window_end_iso=_iso(end),
            incident=incident,
        )

        try:
            pack.metrics_summary = self._gather_metrics(service, start, end)
            pack.sources_ok["prometheus"] = True
        except Exception as exc:
            pack.sources_ok["prometheus"] = False
            pack.gather_errors.append(f"prometheus: {exc}")
            logger.warning("metrics gather failed: %s", exc)

        try:
            pack.error_logs = self._gather_logs(service, start, end)
            pack.sources_ok["loki"] = True
        except Exception as exc:
            pack.sources_ok["loki"] = False
            pack.gather_errors.append(f"loki: {exc}")
            logger.warning("logs gather failed: %s", exc)

        try:
            pack.traces = self._gather_traces(service, start, end)
            pack.sources_ok["tempo"] = True
        except Exception as exc:
            pack.sources_ok["tempo"] = False
            pack.gather_errors.append(f"tempo: {exc}")
            logger.warning("traces gather failed: %s", exc)

        # Enrich logs with extracted trace_id; pick best primary for deep-link
        pack.error_logs = [_enrich_log_trace_id(row) for row in pack.error_logs]
        pack.primary_trace_id = _select_primary_trace_id(pack)

        # --- Topology neighborhood + neighbor evidence expansion ---
        self._attach_topology_and_neighbors(pack, service, start, end)

        # Change/chaos markers from primary + neighbor logs (deploy/chaos context)
        pack.change_events = _extract_change_events(
            pack.error_logs + pack.neighbor_logs
        )

        logger.info(
            "evidence gathered incident=%s service=%s prom=%s loki_lines=%s "
            "traces=%s neighbors=%s primary_trace_id=%s errors=%s",
            pack.incident_id,
            service,
            pack.sources_ok.get("prometheus"),
            len(pack.error_logs),
            len(pack.traces),
            (pack.topology or {}).get("upstream"),
            pack.primary_trace_id,
            pack.gather_errors,
        )
        return pack

    def _attach_topology_and_neighbors(
        self,
        pack: EvidencePack,
        service: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """
        Resolve static topology (+ Tempo-inferred edges) and gather RED/logs
        for upstream/downstream neighbors so RCA can blame dependency roots.
        """
        if not settings.enable_topology_expand:
            pack.topology = {"service": service, "source": "disabled"}
            return

        catalog = load_topology_catalog(settings.topology_path or None)
        edges = infer_edges_from_traces(service, pack.traces)
        nb = catalog.with_inferred_edges(service, edges)
        pack.topology = nb.to_dict()

        neighbor_metrics: dict[str, Any] = {}
        neighbor_logs: list[dict[str, Any]] = []
        neighbor_traces: list[dict[str, Any]] = []

        for peer in nb.all_neighbors():
            try:
                m = self._gather_metrics(peer, start, end)
                instant = (m or {}).get("instant") or {}
                neighbor_metrics[peer] = {
                    "instant": instant,
                    "relation": (
                        "upstream"
                        if peer in nb.upstream
                        else "downstream"
                        if peer in nb.downstream
                        else "peer"
                    ),
                }
            except Exception as exc:
                pack.gather_errors.append(f"neighbor_metrics:{peer}: {exc}")

            try:
                logs = self._gather_logs(peer, start, end)
                for row in logs[: settings.max_neighbor_log_lines]:
                    row = _enrich_log_trace_id(dict(row))
                    labels = dict(row.get("labels") or {})
                    labels.setdefault("service_name", peer)
                    labels["topology_relation"] = (
                        "upstream" if peer in nb.upstream else "downstream"
                    )
                    row["labels"] = labels
                    row["neighbor_service"] = peer
                    neighbor_logs.append(row)
            except Exception as exc:
                pack.gather_errors.append(f"neighbor_logs:{peer}: {exc}")

            try:
                trs = self._gather_traces(peer, start, end)
                for tr in trs[: settings.max_neighbor_traces]:
                    tr = dict(tr)
                    tr["neighbor_service"] = peer
                    neighbor_traces.append(tr)
            except Exception as exc:
                pack.gather_errors.append(f"neighbor_traces:{peer}: {exc}")

        pack.neighbor_metrics = neighbor_metrics
        pack.neighbor_logs = neighbor_logs
        pack.neighbor_traces = neighbor_traces
        pack.sources_ok["topology"] = True

    # ------------------------------------------------------------------
    # Prometheus
    # ------------------------------------------------------------------

    def _prom_query(self, promql: str, ts: Optional[float] = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"query": promql}
        if ts is not None:
            params["time"] = ts
        r = self._http.get(f"{self.prometheus_url}/api/v1/query", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prom status={body.get('status')}")
        return body.get("data", {}).get("result", []) or []

    def _prom_query_range(
        self, promql: str, start: datetime, end: datetime, step: str = "60s"
    ) -> list[dict[str, Any]]:
        params = {
            "query": promql,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        r = self._http.get(f"{self.prometheus_url}/api/v1/query_range", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"prom range status={body.get('status')}")
        return body.get("data", {}).get("result", []) or []

    def _first_scalar(self, templates: list[str], service: str) -> Optional[float]:
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
                if val != val:  # NaN
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
        return {"promql": None, "points": 0, "note": "no series matched templates"}

    def _gather_metrics(
        self, service: str, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """
        RED-ish summary for the service over the evidence window.

        Templates prefer demo_http_* (checkout/payment) then OTel HTTP server
        metrics (service_name label from LGTM).
        """
        error_rate_q = [
            (
                'sum(rate(demo_http_requests_total{{service_name="{svc}",status="error"}}[2m])) '
                '/ clamp_min(sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m])), 1e-9)'
            ),
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
            # Astronomy Shop / gRPC
            (
                'sum(rate(rpc_server_duration_milliseconds_count{{service_name="{svc}",'
                'rpc_grpc_status_code!="0"}}[2m])) '
                '/ clamp_min(sum(rate(rpc_server_duration_milliseconds_count'
                '{{service_name="{svc}"}}[2m])), 1e-9)'
            ),
        ]
        request_rate_q = [
            'sum(rate(demo_http_requests_total{{service_name="{svc}"}}[2m]))',
            'sum(rate(http_server_duration_milliseconds_count{{service_name="{svc}"}}[2m]))',
            'sum(rate(http_server_request_duration_seconds_count{{service_name="{svc}"}}[2m]))',
            'sum(rate(rpc_server_duration_milliseconds_count{{service_name="{svc}"}}[2m]))',
        ]
        latency_p95_q = [
            (
                "histogram_quantile(0.95, sum(rate(demo_http_duration_ms_bucket"
                '{{service_name="{svc}"}}[2m])) by (le)) / 1000'
            ),
            (
                "histogram_quantile(0.95, sum(rate(http_server_duration_milliseconds_bucket"
                '{{service_name="{svc}"}}[2m])) by (le)) / 1000'
            ),
            (
                "histogram_quantile(0.95, sum(rate(rpc_server_duration_milliseconds_bucket"
                '{{service_name="{svc}"}}[2m])) by (le)) / 1000'
            ),
        ]
        # Status code breakdown (instant)
        status_breakdown: list[dict[str, Any]] = []
        for q in (
            f'sum by (http_status_code) (rate(http_server_duration_milliseconds_count{{service_name="{service}"}}[5m]))',
            f'sum by (status) (rate(demo_http_requests_total{{service_name="{service}"}}[5m]))',
        ):
            try:
                for row in self._prom_query(q):
                    metric = row.get("metric") or {}
                    code = metric.get("http_status_code") or metric.get("status") or "?"
                    try:
                        val = float(row["value"][1])
                    except Exception:
                        continue
                    status_breakdown.append({"code": str(code), "rate": round(val, 6)})
                if status_breakdown:
                    break
            except Exception:
                continue

        return {
            "service": service,
            "window": {"start": _iso(start), "end": _iso(end)},
            "instant": {
                "http_error_rate": self._first_scalar(error_rate_q, service),
                "http_request_rate": self._first_scalar(request_rate_q, service),
                "http_latency_p95_seconds": self._first_scalar(latency_p95_q, service),
            },
            "range": {
                "http_error_rate": self._series_stats(error_rate_q, service, start, end),
                "http_request_rate": self._series_stats(request_rate_q, service, start, end),
                "http_latency_p95_seconds": self._series_stats(
                    latency_p95_q, service, start, end
                ),
            },
            "status_code_rates": status_breakdown[:12],
        }

    # ------------------------------------------------------------------
    # Loki (LogQL)
    # ------------------------------------------------------------------

    def _gather_logs(
        self, service: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """
        Top error-ish log lines for the service.

        LogQL strategies (first that returns data wins):
          1. {service_name="…"} |~ "(?i)error|exception|fail|5[0-9]{2}"
          2. {service_name="…"}
          3. {job=~".*service.*"}
        """
        start_ns = int(start.timestamp() * 1e9)
        end_ns = int(end.timestamp() * 1e9)
        limit = settings.max_log_lines

        short = service.replace("-service", "")
        queries = [
            f'{{service_name="{service}"}} |~ "(?i)error|exception|traceback|failed|5[0-9]{{2}}|fault_mode|pool|timeout"',
            f'{{service_name="{service}"}}',
            f'{{service_name="{short}"}} |~ "(?i)error|exception|fail|fault_mode"',
            f'{{job=~".*{service}.*"}} |~ "(?i)error|exception|fail|fault_mode"',
            f'{{job=~".*{short}.*"}} |~ "(?i)error|exception|fail|fault_mode"',
            f'{{service="{service}"}} |~ "(?i)error|exception|fail|fault_mode"',
            f'{{container=~".*{short}.*"}} |~ "(?i)error|exception|fail|fault_mode"',
            f'{{compose_service=~".*{short}.*"}} |~ "(?i)error|exception|fail|fault_mode"',
            # OTel resource attribute sometimes exported as label
            f'{{exporter="OTLP"}} |~ "(?i){short}" |~ "(?i)error|fault_mode|pool"',
            # Fallback: any error lines in window (still better than inventing)
            '{job=~".+"} |~ "(?i)error|exception|traceback|fault_mode|pool exhaust"',
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
        self, logql: str, start_ns: int, end_ns: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        params = {
            "query": logql,
            "start": str(start_ns),
            "end": str(end_ns),
            "limit": str(limit),
            "direction": "backward",
        }
        r = self._http.get(f"{self.loki_url}/loki/api/v1/query_range", params=params)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"loki status={body.get('status')}")
        out: list[dict[str, Any]] = []
        results = (body.get("data") or {}).get("result") or []
        for stream in results:
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
                                "container",
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
    # Tempo (TraceQL / search)
    # ------------------------------------------------------------------

    def _gather_traces(
        self, service: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """
        Slow / error traces for the service via Tempo search API.

        TraceQL (preferred when supported):
          { resource.service.name="checkout-service" && status=error }
          { resource.service.name="checkout-service" && duration > 500ms }
        Tag search fallback uses service.name.
        """
        limit = settings.max_traces
        min_start = int(start.timestamp())
        max_start = int(end.timestamp())
        # Tempo expects start/end as unix seconds for /api/search
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
            (
                "tags_any",
                {
                    "tags": f'service.name="{service}"',
                    "limit": limit,
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
                    logger.debug("tempo search %s status=%s", mode, r.status_code)
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
                        "root_name": tr.get("rootTraceName") or tr.get("rootTraceName"),
                        "duration_ms": tr.get("durationMs"),
                        "start_time_unix_nano": tr.get("startTimeUnixNano"),
                        "search_mode": mode,
                    }
                )
                if len(collected) >= limit:
                    return collected
        return collected


def _extract_trace_id(line: str, labels: Optional[dict] = None) -> Optional[str]:
    labels = labels or {}
    for k in ("trace_id", "traceID", "traceId"):
        if labels.get(k):
            return str(labels[k])
    m = _TRACE_ID_RE.search(line or "")
    if m:
        return m.group(1).lower()
    # Last resort: 32-hex (W3C trace id) in the line
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


def _select_primary_trace_id(pack: EvidencePack) -> Optional[str]:
    """Prefer slowest Tempo trace; else first log-correlated trace_id."""
    pool = list(pack.traces or []) + list(pack.neighbor_traces or [])
    if pool:
        ranked = sorted(
            pool,
            key=lambda t: (t.get("duration_ms") or 0),
            reverse=True,
        )
        tid = ranked[0].get("trace_id")
        if tid:
            return str(tid)
    for row in list(pack.error_logs or []) + list(pack.neighbor_logs or []):
        if row.get("trace_id"):
            return str(row["trace_id"])
    return None


def _extract_change_events(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best-effort deploy/chaos markers for RCA change context."""
    out: list[dict[str, Any]] = []
    for row in logs or []:
        line = row.get("line") or ""
        m = _CHANGE_RE.search(line)
        if not m:
            continue
        out.append(
            {
                "type": m.group(1).lower(),
                "message": line[:240],
                "service": (row.get("labels") or {}).get("service_name")
                or row.get("neighbor_service"),
                "source": "log_derived",
            }
        )
        if len(out) >= 15:
            break
    return out
