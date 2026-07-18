"""SQLite persistence for on-call feedback."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings
from app.models import FeedbackRecord, FeedbackStats, utc_now

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


def _bool_to_int(v: Optional[bool]) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def _int_to_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    return bool(int(v))


class FeedbackRepository:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.feedback_db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
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
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    anomaly_correct INTEGER,
                    rca_useful INTEGER,
                    action_effective INTEGER,
                    comment TEXT NOT NULL DEFAULT '',
                    reviewer TEXT NOT NULL DEFAULT 'oncall',
                    corrected_root_cause TEXT,
                    service_name TEXT,
                    severity TEXT,
                    incident_status TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fb_incident ON feedback(incident_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fb_created ON feedback(created_at)"
            )
        logger.info("feedback sqlite ready path=%s", self.db_path)

    def insert(self, rec: FeedbackRecord) -> FeedbackRecord:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO feedback (
                    id, incident_id, anomaly_correct, rca_useful, action_effective,
                    comment, reviewer, corrected_root_cause,
                    service_name, severity, incident_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.id,
                    rec.incident_id,
                    _bool_to_int(rec.anomaly_correct),
                    _bool_to_int(rec.rca_useful),
                    _bool_to_int(rec.action_effective),
                    rec.comment or "",
                    rec.reviewer,
                    rec.corrected_root_cause,
                    rec.service_name,
                    rec.severity,
                    rec.incident_status,
                    _iso(rec.created_at),
                ),
            )
        return rec

    def get(self, feedback_id: str) -> Optional[FeedbackRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM feedback WHERE id=?", (feedback_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list(
        self,
        incident_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[FeedbackRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id:
            clauses.append("incident_id=?")
            params.append(incident_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM feedback {where} ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_false_positives(self, limit: int = 100) -> list[FeedbackRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM feedback
                WHERE anomaly_correct = 0
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def compute_stats(self) -> FeedbackStats:
        with self._conn() as conn:
            total = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"]
            rows = conn.execute(
                """
                SELECT anomaly_correct, rca_useful, action_effective
                FROM feedback
                """
            ).fetchall()

        stats = FeedbackStats(total=int(total or 0))
        thumbs_up = 0
        thumbs_total = 0

        for r in rows:
            for col, pos_attr, with_attr in (
                ("anomaly_correct", "anomaly_positive", "with_anomaly_vote"),
                ("rca_useful", "rca_positive", "with_rca_vote"),
                ("action_effective", "action_positive", "with_action_vote"),
            ):
                v = r[col]
                if v is None:
                    continue
                setattr(stats, with_attr, getattr(stats, with_attr) + 1)
                thumbs_total += 1
                if int(v) == 1:
                    setattr(stats, pos_attr, getattr(stats, pos_attr) + 1)
                    thumbs_up += 1
                elif col == "anomaly_correct" and int(v) == 0:
                    stats.false_positive_count += 1

        if thumbs_total > 0:
            stats.feedback_positive_rate = round(thumbs_up / thumbs_total, 4)
        if stats.with_rca_vote > 0:
            stats.rca_accuracy_estimate = round(
                stats.rca_positive / stats.with_rca_vote, 4
            )
        if stats.with_anomaly_vote > 0:
            stats.anomaly_precision_estimate = round(
                stats.anomaly_positive / stats.with_anomaly_vote, 4
            )
        if stats.with_action_vote > 0:
            stats.action_success_rate = round(
                stats.action_positive / stats.with_action_vote, 4
            )
        return stats

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> FeedbackRecord:
        return FeedbackRecord(
            id=row["id"],
            incident_id=row["incident_id"],
            anomaly_correct=_int_to_bool(row["anomaly_correct"]),
            rca_useful=_int_to_bool(row["rca_useful"]),
            action_effective=_int_to_bool(row["action_effective"]),
            comment=row["comment"] or "",
            reviewer=row["reviewer"] or "oncall",
            corrected_root_cause=row["corrected_root_cause"],
            service_name=row["service_name"],
            severity=row["severity"],
            incident_status=row["incident_status"],
            created_at=_parse_dt(row["created_at"]) or utc_now(),
        )
