"""
SQLite persistence for incidents.

Production choices:
- SQLite file on a Docker volume is perfect for a laptop CV demo (zero ops).
- WAL mode improves concurrent readers during API + worker writes.
- JSON columns for labels/context avoid premature schema explosion.
- Real production: Postgres, multi-AZ, encrypted at rest, automated backups.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from aiops_shared.models import AnomalySeverity, Incident, IncidentStatus, utc_now

from app.config import settings

logger = logging.getLogger(__name__)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


class IncidentRepository:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.incident_db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    source_anomaly_id TEXT,
                    metric_name TEXT,
                    metric_value REAL,
                    threshold REAL,
                    labels_json TEXT NOT NULL DEFAULT '{}',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    root_cause TEXT,
                    rca_confidence REAL,
                    remediation_notes TEXT,
                    human_feedback TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    resolved_at TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incidents_service ON incidents(service_name)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_incidents_created ON incidents(created_at)"
            )
        logger.info("sqlite schema ready path=%s", self.db_path)

    def insert(self, incident: Incident) -> Incident:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO incidents (
                    id, title, description, status, severity, service_name,
                    source_anomaly_id, metric_name, metric_value, threshold,
                    labels_json, context_json, root_cause, rca_confidence,
                    remediation_notes, human_feedback,
                    created_at, updated_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    incident.id,
                    incident.title,
                    incident.description,
                    incident.status.value,
                    incident.severity.value,
                    incident.service_name,
                    incident.source_anomaly_id,
                    incident.metric_name,
                    incident.metric_value,
                    incident.threshold,
                    json.dumps(incident.labels),
                    json.dumps(incident.context),
                    incident.root_cause,
                    incident.rca_confidence,
                    incident.remediation_notes,
                    incident.human_feedback,
                    _iso(incident.created_at),
                    _iso(incident.updated_at),
                    _iso(incident.resolved_at),
                ),
            )
        return incident

    def get(self, incident_id: str) -> Optional[Incident]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def list(
        self,
        status: Optional[str] = None,
        service_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[Incident]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if service_name:
            clauses.append("service_name = ?")
            params.append(service_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM incidents {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 200)))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_incident(r) for r in rows]

    def update(self, incident: Incident) -> Incident:
        incident.touch()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE incidents SET
                    title = ?, description = ?, status = ?, severity = ?,
                    service_name = ?, source_anomaly_id = ?, metric_name = ?,
                    metric_value = ?, threshold = ?, labels_json = ?,
                    context_json = ?, root_cause = ?, rca_confidence = ?,
                    remediation_notes = ?, human_feedback = ?,
                    updated_at = ?, resolved_at = ?
                WHERE id = ?
                """,
                (
                    incident.title,
                    incident.description,
                    incident.status.value,
                    incident.severity.value,
                    incident.service_name,
                    incident.source_anomaly_id,
                    incident.metric_name,
                    incident.metric_value,
                    incident.threshold,
                    json.dumps(incident.labels),
                    json.dumps(incident.context),
                    incident.root_cause,
                    incident.rca_confidence,
                    incident.remediation_notes,
                    incident.human_feedback,
                    _iso(incident.updated_at),
                    _iso(incident.resolved_at),
                    incident.id,
                ),
            )
        return incident

    def find_open_correlated(
        self,
        service_name: str,
        metric_name: Optional[str],
        window_minutes: int,
    ) -> Optional[Incident]:
        """Dedup: reuse open incident for same service+metric within the window."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE service_name = ?
                  AND IFNULL(metric_name, '') = IFNULL(?, '')
                  AND status IN ('open', 'acknowledged', 'investigating', 'remediating')
                  AND datetime(created_at) >= datetime('now', ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    service_name,
                    metric_name or "",
                    f"-{window_minutes} minutes",
                ),
            ).fetchone()
        return self._row_to_incident(row) if row else None

    def count_by_status(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM incidents GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        return Incident(
            id=row["id"],
            title=row["title"],
            description=row["description"] or "",
            status=IncidentStatus(row["status"]),
            severity=AnomalySeverity(row["severity"]),
            service_name=row["service_name"],
            source_anomaly_id=row["source_anomaly_id"],
            metric_name=row["metric_name"],
            metric_value=row["metric_value"],
            threshold=row["threshold"],
            labels=json.loads(row["labels_json"] or "{}"),
            context=json.loads(row["context_json"] or "{}"),
            root_cause=row["root_cause"],
            rca_confidence=row["rca_confidence"],
            remediation_notes=row["remediation_notes"],
            human_feedback=row["human_feedback"],
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or utc_now(),
            resolved_at=_parse_dt(row["resolved_at"]),
        )


def incident_from_anomaly(anomaly) -> Incident:
    """Map AnomalyEvent → Incident ticket."""
    from aiops_shared.models import AnomalyEvent

    assert isinstance(anomaly, AnomalyEvent)
    title = f"[{anomaly.severity.value.upper()}] {anomaly.service_name}: {anomaly.metric_name}"
    return Incident(
        title=title,
        description=anomaly.message
        or f"Anomaly on {anomaly.metric_name}={anomaly.metric_value}",
        severity=anomaly.severity,
        service_name=anomaly.service_name,
        source_anomaly_id=anomaly.id,
        metric_name=anomaly.metric_name,
        metric_value=anomaly.metric_value,
        threshold=anomaly.threshold,
        labels=dict(anomaly.labels),
        context={
            **anomaly.context,
            "detector": anomaly.detector,
            "detected_at": anomaly.detected_at.isoformat(),
            "schema_version": anomaly.schema_version,
        },
    )
