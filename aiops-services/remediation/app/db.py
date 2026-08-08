"""SQLite persistence for remediation action history."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from app.config import settings
from app.models import ActionRecord, ActionStatus, RiskLevel, utc_now

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


class ActionRepository:
    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or settings.remediation_db_path
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
                CREATE TABLE IF NOT EXISTS remediation_actions (
                    id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_text TEXT NOT NULL DEFAULT '',
                    target_service TEXT NOT NULL DEFAULT '',
                    risk_level TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result TEXT,
                    executed_by TEXT,
                    command TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    executed_at TEXT
                )
                """
            )
            self._ensure_column(
                conn,
                "remediation_actions",
                "verification_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                conn,
                "remediation_actions",
                "rollback_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS remediation_resource_locks (
                    resource_key TEXT PRIMARY KEY,
                    owner_action_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rem_incident "
                "ON remediation_actions(incident_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rem_status "
                "ON remediation_actions(status)"
            )
        logger.info("remediation sqlite ready path=%s", self.db_path)

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def insert(self, rec: ActionRecord) -> ActionRecord:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO remediation_actions (
                    id, incident_id, action_type, action_text, target_service,
                    risk_level, status, payload_json, result, executed_by,
                    command, created_at, updated_at, executed_at,
                    verification_json, rollback_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.id,
                    rec.incident_id,
                    rec.action_type,
                    rec.action_text,
                    rec.target_service,
                    rec.risk_level.value,
                    rec.status.value,
                    json.dumps(rec.payload),
                    rec.result,
                    rec.executed_by,
                    rec.command,
                    _iso(rec.created_at),
                    _iso(rec.updated_at),
                    _iso(rec.executed_at),
                    json.dumps(rec.verification),
                    json.dumps(rec.rollback),
                ),
            )
        return rec

    def update(self, rec: ActionRecord) -> ActionRecord:
        rec.updated_at = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE remediation_actions SET
                    action_type=?, action_text=?, target_service=?,
                    risk_level=?, status=?, payload_json=?, result=?,
                    executed_by=?, command=?, updated_at=?, executed_at=?,
                    verification_json=?, rollback_json=?
                WHERE id=?
                """,
                (
                    rec.action_type,
                    rec.action_text,
                    rec.target_service,
                    rec.risk_level.value,
                    rec.status.value,
                    json.dumps(rec.payload),
                    rec.result,
                    rec.executed_by,
                    rec.command,
                    _iso(rec.updated_at),
                    _iso(rec.executed_at),
                    json.dumps(rec.verification),
                    json.dumps(rec.rollback),
                    rec.id,
                ),
            )
        return rec

    def get(self, action_id: str) -> Optional[ActionRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM remediation_actions WHERE id=?", (action_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list(
        self,
        incident_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> list[ActionRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if incident_id:
            clauses.append("incident_id=?")
            params.append(incident_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM remediation_actions {where} "
            "ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 200)))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM remediation_actions GROUP BY status"
            ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def try_acquire_resource_lock(
        self,
        resource_key: str,
        owner_action_id: str,
        ttl_sec: int,
    ) -> tuple[bool, Optional[str]]:
        """Atomically acquire a cross-thread/process resource lock."""
        now = utc_now()
        expires = now + timedelta(seconds=max(1, ttl_sec))
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM remediation_resource_locks WHERE expires_at <= ?",
                (_iso(now),),
            )
            row = conn.execute(
                "SELECT owner_action_id FROM remediation_resource_locks WHERE resource_key=?",
                (resource_key,),
            ).fetchone()
            if row:
                return False, str(row["owner_action_id"])
            conn.execute(
                """
                INSERT INTO remediation_resource_locks(
                    resource_key, owner_action_id, acquired_at, expires_at
                ) VALUES (?, ?, ?, ?)
                """,
                (resource_key, owner_action_id, _iso(now), _iso(expires)),
            )
        return True, None

    def release_resource_lock(self, resource_key: str, owner_action_id: str) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """
                DELETE FROM remediation_resource_locks
                WHERE resource_key=? AND owner_action_id=?
                """,
                (resource_key, owner_action_id),
            )
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ActionRecord:
        return ActionRecord(
            id=row["id"],
            incident_id=row["incident_id"],
            action_type=row["action_type"],
            action_text=row["action_text"] or "",
            target_service=row["target_service"] or "",
            risk_level=RiskLevel(row["risk_level"]),
            status=ActionStatus(row["status"]),
            payload=json.loads(row["payload_json"] or "{}"),
            result=row["result"],
            executed_by=row["executed_by"],
            command=row["command"],
            verification=json.loads(row["verification_json"] or "{}"),
            rollback=json.loads(row["rollback_json"] or "{}"),
            created_at=_parse_dt(row["created_at"]) or utc_now(),
            updated_at=_parse_dt(row["updated_at"]) or utc_now(),
            executed_at=_parse_dt(row["executed_at"]),
        )
