"""SQLite persistence for Engine QA reviews."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings
from app.models import QAReview, utc_now

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


def _b2i(v: Optional[bool]) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def _i2b(v: Any) -> Optional[bool]:
    if v is None:
        return None
    return bool(int(v))


class QARepository:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.qa_db_path
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
                CREATE TABLE IF NOT EXISTS qa_reviews (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    anomaly_id TEXT,
                    decision_id TEXT,
                    anomaly_correct INTEGER,
                    confidence_reasonable INTEGER,
                    rca_useful INTEGER,
                    decision_correct INTEGER,
                    expected_confidence REAL,
                    corrected_root_cause TEXT,
                    llm_hallucinated INTEGER,
                    decision_action TEXT,
                    decision_iterations INTEGER,
                    engine_confidence REAL,
                    llm_confidence REAL,
                    comment TEXT NOT NULL DEFAULT '',
                    reviewer TEXT NOT NULL DEFAULT 'oncall-sre',
                    service_name TEXT,
                    severity TEXT,
                    metric_name TEXT,
                    detection_method TEXT,
                    missing_context TEXT NOT NULL DEFAULT '[]',
                    confidence_breakdown TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_qa_incident ON qa_reviews(incident_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_qa_created ON qa_reviews(created_at)"
            )
        logger.info("engine-qa sqlite ready path=%s", self.db_path)

    def insert(self, rec: QAReview) -> QAReview:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO qa_reviews (
                    id, incident_id, anomaly_id, decision_id,
                    anomaly_correct, confidence_reasonable, rca_useful, decision_correct,
                    expected_confidence, corrected_root_cause, llm_hallucinated,
                    decision_action, decision_iterations, engine_confidence, llm_confidence,
                    comment, reviewer, service_name, severity, metric_name,
                    detection_method, missing_context, confidence_breakdown, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.id,
                    rec.incident_id,
                    rec.anomaly_id,
                    rec.decision_id,
                    _b2i(rec.anomaly_correct),
                    _b2i(rec.confidence_reasonable),
                    _b2i(rec.rca_useful),
                    _b2i(rec.decision_correct),
                    rec.expected_confidence,
                    rec.corrected_root_cause,
                    _b2i(rec.llm_hallucinated),
                    rec.decision_action,
                    rec.decision_iterations,
                    rec.engine_confidence,
                    rec.llm_confidence,
                    rec.comment or "",
                    rec.reviewer,
                    rec.service_name,
                    rec.severity,
                    rec.metric_name,
                    rec.detection_method,
                    json.dumps(rec.missing_context or []),
                    json.dumps(rec.confidence_breakdown or {}),
                    _iso(rec.created_at or utc_now()),
                ),
            )
        return rec

    def list_reviews(self, limit: int = 50) -> list[QAReview]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM qa_reviews ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_review(r) for r in rows]

    def list_all(self) -> list[QAReview]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM qa_reviews ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_review(r) for r in rows]

    def get(self, review_id: str) -> Optional[QAReview]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM qa_reviews WHERE id = ?", (review_id,)
            ).fetchone()
        return self._row_to_review(row) if row else None

    def count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM qa_reviews").fetchone()[0])

    def _row_to_review(self, row: sqlite3.Row) -> QAReview:
        missing = json.loads(row["missing_context"] or "[]")
        breakdown = json.loads(row["confidence_breakdown"] or "{}")
        return QAReview(
            id=row["id"],
            incident_id=row["incident_id"],
            anomaly_id=row["anomaly_id"],
            decision_id=row["decision_id"],
            anomaly_correct=_i2b(row["anomaly_correct"]),
            confidence_reasonable=_i2b(row["confidence_reasonable"]),
            rca_useful=_i2b(row["rca_useful"]),
            decision_correct=_i2b(row["decision_correct"]),
            expected_confidence=row["expected_confidence"],
            corrected_root_cause=row["corrected_root_cause"],
            llm_hallucinated=_i2b(row["llm_hallucinated"]),
            decision_action=row["decision_action"],
            decision_iterations=row["decision_iterations"],
            engine_confidence=row["engine_confidence"],
            llm_confidence=row["llm_confidence"],
            comment=row["comment"] or "",
            reviewer=row["reviewer"] or "oncall-sre",
            service_name=row["service_name"],
            severity=row["severity"],
            metric_name=row["metric_name"],
            detection_method=row["detection_method"],
            missing_context=missing if isinstance(missing, list) else [],
            confidence_breakdown=breakdown if isinstance(breakdown, dict) else {},
            created_at=_parse_dt(row["created_at"]) or utc_now(),
        )
