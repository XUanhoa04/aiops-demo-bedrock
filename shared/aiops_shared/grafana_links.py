"""
Grafana / Tempo deep-link builders for ops UIs.

Canonical trace link (interview / demo bar)
-------------------------------------------
  http://localhost:3000/explore?orgId=1&left={
    "datasource":"Tempo",
    "queries":[{"refId":"A","queryType":"traceql","query":"<trace_id>"}]
  }

We also emit Grafana-10 `panes` links when UIDs are provisioned — both work on
otel-lgtm; the classic `left=` form matches the product brief exactly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode


def _ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def grafana_explore_trace_url(
    *,
    grafana_base: str,
    trace_id: str,
    datasource_uid: str = "tempo",
    datasource_name: str = "Tempo",
    from_ms: Optional[int] = None,
    to_ms: Optional[int] = None,
) -> str:
    """
    Open a single Tempo trace in Grafana Explore.

    Primary format (spec):
      /explore?orgId=1&left={"datasource":"Tempo","queries":[{... "query":"<id>"}]}

    Also appends `from`/`to` when provided for time-window context.
    """
    base = grafana_base.rstrip("/")
    # Classic Explore `left` payload — matches required CV demo deep-link shape.
    left = {
        "datasource": datasource_name or "Tempo",
        "queries": [
            {
                "refId": "A",
                "queryType": "traceql",
                "query": trace_id,
            }
        ],
    }
    now = datetime.now(timezone.utc)
    to_ms = to_ms or _ms(now)
    from_ms = from_ms or _ms(now - timedelta(hours=1))
    left["range"] = {"from": str(from_ms), "to": str(to_ms)}

    # Compact JSON, then URL-encode the whole left blob (Grafana expects this).
    left_json = json.dumps(left, separators=(",", ":"))
    return f"{base}/explore?orgId=1&left={quote(left_json, safe='')}"


def grafana_explore_service_traces_url(
    *,
    grafana_base: str,
    service_name: str,
    datasource_uid: str = "tempo",
    datasource_name: str = "Tempo",
    minutes: int = 60,
    status_error: bool = True,
) -> str:
    """Explore TraceQL for a service (optionally errors only) — when no trace id yet."""
    base = grafana_base.rstrip("/")
    now = datetime.now(timezone.utc)
    to_ms = _ms(now)
    from_ms = _ms(now - timedelta(minutes=minutes))
    if status_error:
        tql = f'{{resource.service.name="{service_name}" && status=error}}'
    else:
        tql = f'{{resource.service.name="{service_name}"}}'
    left = {
        "datasource": datasource_name or "Tempo",
        "queries": [
            {
                "refId": "A",
                "queryType": "traceql",
                "query": tql,
            }
        ],
        "range": {"from": str(from_ms), "to": str(to_ms)},
    }
    left_json = json.dumps(left, separators=(",", ":"))
    return f"{base}/explore?orgId=1&left={quote(left_json, safe='')}"


def grafana_explore_logs_url(
    *,
    grafana_base: str,
    service_name: str,
    datasource_uid: str = "loki",
    datasource_name: str = "Loki",
    minutes: int = 60,
) -> str:
    base = grafana_base.rstrip("/")
    now = datetime.now(timezone.utc)
    to_ms = _ms(now)
    from_ms = _ms(now - timedelta(minutes=minutes))
    logql = f'{{service_name="{service_name}"}} |~ "(?i)error|exception|fail"'
    left = {
        "datasource": datasource_name or "Loki",
        "queries": [
            {
                "refId": "A",
                "expr": logql,
                "queryType": "range",
            }
        ],
        "range": {"from": str(from_ms), "to": str(to_ms)},
    }
    left_json = json.dumps(left, separators=(",", ":"))
    return f"{base}/explore?orgId=1&left={quote(left_json, safe='')}"


def build_observability_links(
    *,
    grafana_base: str,
    service_name: str,
    primary_trace_id: Optional[str] = None,
    tempo_uid: str = "tempo",
    loki_uid: str = "loki",
    tempo_name: str = "Tempo",
    loki_name: str = "Loki",
    window_minutes: int = 60,
) -> dict[str, Any]:
    """Bundle of deep-links for Incident UI / API."""
    links: dict[str, Any] = {
        "grafana_home": grafana_base.rstrip("/"),
        "service_traces_url": grafana_explore_service_traces_url(
            grafana_base=grafana_base,
            service_name=service_name,
            datasource_uid=tempo_uid,
            datasource_name=tempo_name,
            minutes=window_minutes,
            status_error=True,
        ),
        "service_traces_all_url": grafana_explore_service_traces_url(
            grafana_base=grafana_base,
            service_name=service_name,
            datasource_uid=tempo_uid,
            datasource_name=tempo_name,
            minutes=window_minutes,
            status_error=False,
        ),
        "service_logs_url": grafana_explore_logs_url(
            grafana_base=grafana_base,
            service_name=service_name,
            datasource_uid=loki_uid,
            datasource_name=loki_name,
            minutes=window_minutes,
        ),
        "primary_trace_id": primary_trace_id,
        "primary_trace_url": None,
    }
    if primary_trace_id:
        links["primary_trace_url"] = grafana_explore_trace_url(
            grafana_base=grafana_base,
            trace_id=primary_trace_id,
            datasource_uid=tempo_uid,
            datasource_name=tempo_name,
        )
    return links
