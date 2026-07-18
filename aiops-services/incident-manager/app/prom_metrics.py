"""
Prometheus exporter metrics for Incident Manager (scraped via GET /metrics).

Exposed series
--------------
  - incidents_created_total{source,severity,service}
      Counter of new incident tickets (not correlation updates).
  - open_incidents
      Gauge of tickets currently in open-ish statuses.
  - incidents_correlated_total{service}
      Noise-reduction: anomalies merged into an existing ticket.
  - incident_manager_errors_total{stage}
      Operational SLIs (db / redis / rca).

Keep label cardinality low (service names from the demo set only).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Info

SERVICE_INFO = Info("aiops_incident_manager", "Incident manager build metadata")
SERVICE_INFO.info({"component": "incident-manager", "version": "0.2.0"})

INCIDENTS_CREATED = Counter(
    "incidents_created_total",
    "Number of incident tickets created",
    ["source", "severity", "service"],
)

OPEN_INCIDENTS = Gauge(
    "open_incidents",
    "Number of incidents currently open (open/ack/investigating/remediating)",
)

INCIDENTS_CORRELATED = Counter(
    "incidents_correlated_total",
    "Anomalies correlated into an existing open incident",
    ["service"],
)

ERRORS_TOTAL = Counter(
    "incident_manager_errors_total",
    "Errors by stage",
    ["stage"],
)


def record_created(source: str, severity: str, service: str) -> None:
    INCIDENTS_CREATED.labels(
        source=source or "unknown",
        severity=severity or "unknown",
        service=service or "unknown",
    ).inc()


def record_correlated(service: str) -> None:
    INCIDENTS_CORRELATED.labels(service=service or "unknown").inc()


def set_open_incidents(count: int) -> None:
    OPEN_INCIDENTS.set(max(0, int(count)))
