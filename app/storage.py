from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain import Session, SessionStatus


@dataclass(frozen=True)
class MessageClaim:
    state: str  # claimed, completed, processing, conflict
    content: str | None = None
    claim_token: str | None = None
    decision_json: str | None = None


class SQLiteStore:
    SCHEMA_VERSION = 2

    def __init__(self, path: str = "data/agent.db") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
              customer_id TEXT PRIMARY KEY, status TEXT NOT NULL,
              abnormal_streak INTEGER NOT NULL, version INTEGER NOT NULL,
              last_outbound_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              customer_id TEXT NOT NULL, message_id TEXT NOT NULL,
              direction TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'processing',
              processing_started_at REAL, completed_at REAL, failure_reason TEXT,
              claim_token TEXT, decision_json TEXT, abnormal_applied INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (customer_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS events (
              action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
              message_id TEXT, action_type TEXT NOT NULL, result TEXT NOT NULL,
              reason TEXT NOT NULL, trace_id TEXT NOT NULL, payload TEXT, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbound_messages (
              action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, sent_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
              action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, content TEXT NOT NULL,
              status TEXT NOT NULL, created_at REAL NOT NULL, accepted_at REAL,
              failure_reason TEXT
            );
            """
        )
        self._migrate_messages_table_if_needed()
        self._ensure_message_columns()
        self._set_schema_version()
        self._conn.commit()

    def _migrate_messages_table_if_needed(self) -> None:
        """Upgrade the original global-message-id table in place for local databases."""
        columns = self._conn.execute("PRAGMA table_info(messages)").fetchall()
        names = {row[1] for row in columns}
        pk_columns = [row[1] for row in columns if row[5]]
        if pk_columns == ["message_id"] or "status" not in names:
            self._conn.execute("ALTER TABLE messages RENAME TO messages_legacy")
            self._conn.execute(
                """
                CREATE TABLE messages (
                  customer_id TEXT NOT NULL, message_id TEXT NOT NULL,
                  direction TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
                  status TEXT NOT NULL DEFAULT 'completed',
                  processing_started_at REAL, completed_at REAL, failure_reason TEXT,
                  claim_token TEXT, decision_json TEXT, abnormal_applied INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (customer_id, message_id)
                )
                """
            )
            legacy_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages_legacy)").fetchall()}
            status_expr = "'completed'" if "status" not in legacy_columns else "COALESCE(status, 'completed')"
            self._conn.execute(
                f"""INSERT OR IGNORE INTO messages
                    (customer_id, message_id, direction, content, created_at, status)
                    SELECT customer_id, message_id, direction, content, created_at, {status_expr}
                    FROM messages_legacy"""
            )
            self._conn.execute("DROP TABLE messages_legacy")

    def _ensure_message_columns(self) -> None:
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
        for name, definition in (("claim_token", "TEXT"), ("decision_json", "TEXT"), ("abnormal_applied", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")

    def _set_schema_version(self) -> None:
        """Keep a monotonic SQLite schema marker for future migrations."""
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version < self.SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    def get_session(self, customer_id: str) -> Session:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE customer_id = ?", (customer_id,)).fetchone()
            if row:
                values = dict(row)
                values["status"] = SessionStatus(values["status"])
                return Session(**values)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                self._conn.execute(
                    "INSERT INTO sessions (customer_id, status, abnormal_streak, version, last_outbound_at, created_at, updated_at) "
                    "VALUES (?, ?, 0, 0, NULL, ?, ?) ON CONFLICT(customer_id) DO NOTHING",
                    (customer_id, SessionStatus.ACTIVE.value, now, now),
                )
                row = self._conn.execute("SELECT * FROM sessions WHERE customer_id = ?", (customer_id,)).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            values = dict(row)
            values["status"] = SessionStatus(values["status"])
            return Session(**values)

    def claim_message(self, message_id: str, customer_id: str, content: str, processing_timeout: float = 120.0) -> MessageClaim:
        """Atomically claim a message, or report completed/in-progress/conflicting work."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT content, status, processing_started_at, claim_token, decision_json FROM messages WHERE customer_id=? AND message_id=?",
                    (customer_id, message_id),
                ).fetchone()
                if row is None:
                    now = time.time()
                    token = str(uuid.uuid4())
                    self._conn.execute(
                        "INSERT INTO messages (customer_id, message_id, direction, content, created_at, status, processing_started_at, claim_token) VALUES (?, ?, 'inbound', ?, ?, 'processing', ?, ?)",
                        (customer_id, message_id, content, now, now, token),
                    )
                    self._conn.commit()
                    return MessageClaim("claimed", content, token)
                if row["content"] != content:
                    self._conn.commit()
                    return MessageClaim("conflict", row["content"])
                status = row["status"]
                started = row["processing_started_at"] or 0.0
                if status == "completed":
                    self._conn.commit()
                    return MessageClaim("completed", row["content"], None, row["decision_json"])
                if status == "processing" and processing_timeout > 0 and time.time() - started <= processing_timeout:
                    self._conn.commit()
                    return MessageClaim("processing", row["content"], row["claim_token"], row["decision_json"])
                now = time.time()
                token = str(uuid.uuid4())
                self._conn.execute(
                    "UPDATE messages SET status='processing', processing_started_at=?, failure_reason=NULL, claim_token=? WHERE customer_id=? AND message_id=?",
                    (now, token, customer_id, message_id),
                )
                self._conn.commit()
                return MessageClaim("claimed", row["content"], token, row["decision_json"])
            except Exception:
                self._conn.rollback()
                raise

    def mark_message_completed(self, customer_id: str, message_id: str, claim_token: str | None = None) -> bool:
        with self._lock:
            where = "customer_id=? AND message_id=? AND status='processing'"
            params: list[Any] = [time.time(), customer_id, message_id]
            if claim_token is not None:
                where += " AND claim_token=?"
                params.append(claim_token)
            cursor = self._conn.execute(f"UPDATE messages SET status='completed', completed_at=?, failure_reason=NULL WHERE {where}", params)
            self._conn.commit()
            return cursor.rowcount == 1

    def mark_message_failed(self, customer_id: str, message_id: str, reason: str, claim_token: str | None = None) -> bool:
        with self._lock:
            where = "customer_id=? AND message_id=? AND status='processing'"
            params: list[Any] = [reason[:500], customer_id, message_id]
            if claim_token is not None:
                where += " AND claim_token=?"
                params.append(claim_token)
            cursor = self._conn.execute(f"UPDATE messages SET status='failed', failure_reason=? WHERE {where}", params)
            self._conn.commit()
            return cursor.rowcount == 1

    def save_decision_once(self, customer_id: str, message_id: str, claim_token: str, decision_json: str, abnormal: bool) -> Session:
        """Persist classification and apply its abnormal contribution exactly once."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                message = self._conn.execute("SELECT abnormal_applied FROM messages WHERE customer_id=? AND message_id=? AND claim_token=? AND status='processing'", (customer_id, message_id, claim_token)).fetchone()
                if not message:
                    self._conn.rollback()
                    raise RuntimeError("message claim lost")
                self._conn.execute("UPDATE messages SET decision_json=? WHERE customer_id=? AND message_id=? AND claim_token=?", (decision_json, customer_id, message_id, claim_token))
                row = self._conn.execute("SELECT * FROM sessions WHERE customer_id=?", (customer_id,)).fetchone()
                if not row:
                    self._conn.rollback()
                    raise RuntimeError("session missing")
                values = dict(row)
                status = SessionStatus(values["status"])
                if not message["abnormal_applied"] and status is SessionStatus.ACTIVE:
                    streak = values["abnormal_streak"] + 1 if abnormal else 0
                    now = time.time()
                    self._conn.execute("UPDATE sessions SET abnormal_streak=?, updated_at=?, version=version+1 WHERE customer_id=?", (streak, now, customer_id))
                    self._conn.execute("UPDATE messages SET abnormal_applied=1 WHERE customer_id=? AND message_id=? AND claim_token=?", (customer_id, message_id, claim_token))
                    values.update(abnormal_streak=streak, version=values["version"] + 1, updated_at=now)
                values["status"] = status
                self._conn.commit()
                return Session(**values)
            except Exception:
                self._conn.rollback()
                raise

    def claim_is_valid(self, customer_id: str, message_id: str, claim_token: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM messages WHERE customer_id=? AND message_id=? AND status='processing' AND claim_token=?", (customer_id, message_id, claim_token)).fetchone()
            return row is not None

    def save_session(self, session: Session, expected_version: int | None = None) -> bool:
        with self._lock:
            where = "customer_id=?"
            params: list[Any] = [session.status.value, session.abnormal_streak, session.version, session.last_outbound_at, session.updated_at, session.customer_id]
            if expected_version is not None:
                where += " AND version=?"
                params.append(expected_version)
            cursor = self._conn.execute(f"UPDATE sessions SET status=?, abnormal_streak=?, version=?, last_outbound_at=?, updated_at=? WHERE {where}", params)
            self._conn.commit()
            return cursor.rowcount == 1

    def reactivate_session(self, customer_id: str, expected_version: int) -> Session | None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                cursor = self._conn.execute("UPDATE sessions SET status=?, abnormal_streak=0, version=version+1, updated_at=? WHERE customer_id=? AND version=?", (SessionStatus.ACTIVE.value, now, customer_id, expected_version))
                if cursor.rowcount != 1:
                    self._conn.rollback()
                    return None
                row = self._conn.execute("SELECT * FROM sessions WHERE customer_id=?", (customer_id,)).fetchone()
                self._conn.commit()
                values = dict(row); values["status"] = SessionStatus(values["status"])
                return Session(**values)
            except Exception:
                self._conn.rollback(); raise

    def apply_abnormal_signal(self, customer_id: str, abnormal: bool) -> Session:
        """Atomically update the shared consecutive-abnormal counter."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM sessions WHERE customer_id=?", (customer_id,)).fetchone()
                if row is None:
                    self._conn.rollback()
                    return self.get_session(customer_id)
                values = dict(row)
                status = SessionStatus(values["status"])
                if status is not SessionStatus.ACTIVE:
                    self._conn.commit()
                    values["status"] = status
                    return Session(**values)
                streak = values["abnormal_streak"] + 1 if abnormal else 0
                now = time.time()
                self._conn.execute(
                    "UPDATE sessions SET abnormal_streak=?, updated_at=?, version=version+1 WHERE customer_id=?",
                    (streak, now, customer_id),
                )
                self._conn.commit()
                values.update(status=status, abnormal_streak=streak, version=values["version"] + 1, updated_at=now)
                return Session(**values)
            except Exception:
                self._conn.rollback()
                raise

    def transition_session(self, customer_id: str, target: SessionStatus) -> bool:
        """Atomically perform an ACTIVE -> terminal transition."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE sessions SET status=?, version=version+1, updated_at=? WHERE customer_id=? AND status=?",
                    (target.value, time.time(), customer_id, SessionStatus.ACTIVE.value),
                )
                self._conn.commit()
                return cursor.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def commit_reply(self, customer_id: str, now: float) -> bool:
        """Commit an outbound reply only while the session is still ACTIVE."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "UPDATE sessions SET last_outbound_at=?, version=version+1, updated_at=? WHERE customer_id=? AND status=?",
                    (now, now, customer_id, SessionStatus.ACTIVE.value),
                )
                self._conn.commit()
                return cursor.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def add_message(self, message_id: str, customer_id: str, direction: str, content: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("INSERT INTO messages (customer_id, message_id, direction, content, created_at, status, completed_at) VALUES (?, ?, ?, ?, ?, 'completed', ?)", (customer_id, message_id, direction, content, time.time(), time.time()))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False

    def record_outbound(self, action_id: str, customer_id: str, content: str) -> bool:
        """Idempotently accept a local outbound into the durable outbox/history."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                row = self._conn.execute("SELECT status, content FROM outbox WHERE action_id=?", (action_id,)).fetchone()
                if row:
                    self._conn.commit()
                    return row["content"] == content and row["status"] in {"pending", "accepted"}
                self._conn.execute("INSERT INTO outbox(action_id, customer_id, content, status, created_at, accepted_at) VALUES (?, ?, ?, 'accepted', ?, ?)", (action_id, customer_id, content, now, now))
                self._conn.execute("INSERT OR IGNORE INTO messages(customer_id, message_id, direction, content, created_at, status, completed_at) VALUES (?, ?, 'outbound', ?, ?, 'completed', ?)", (customer_id, action_id, content, now, now))
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback(); raise

    def record_outbound_failure(self, action_id: str, customer_id: str, content: str, reason: str) -> None:
        with self._lock:
            now = time.time()
            self._conn.execute("INSERT INTO outbox(action_id, customer_id, content, status, created_at, failure_reason) VALUES (?, ?, ?, 'failed', ?, ?) ON CONFLICT(action_id) DO UPDATE SET status='failed', failure_reason=excluded.failure_reason", (action_id, customer_id, content, now, reason[:500]))
            self._conn.commit()

    def get_history(self, customer_id: str, limit: int = 6) -> list[dict[str, str]]:
        """Return a small, ordered context window without exposing internal events."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT direction, content FROM messages WHERE customer_id=? ORDER BY rowid DESC LIMIT ?",
                (customer_id, limit),
            ).fetchall()
        return [
            {"role": "user" if row["direction"] == "inbound" else "assistant", "text": row["content"]}
            for row in reversed(rows)
        ]

    def get_history_records(self, customer_id: str, limit: int = 40) -> list[dict[str, Any]]:
        """Return display-safe conversation records for an authenticated operator UI."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id, direction, content, created_at FROM messages "
                "WHERE customer_id=? ORDER BY rowid DESC LIMIT ?",
                (customer_id, limit),
            ).fetchall()
        return [
            {
                "message_id": row["message_id"],
                "role": "user" if row["direction"] == "inbound" else "assistant",
                "text": row["content"],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]

    def add_event(self, action_id: str, customer_id: str, message_id: str | None, action_type: str, result: str, reason: str, trace_id: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (action_id, customer_id, message_id, action_type, result, reason, trace_id, json.dumps(payload or {}), time.time()))
            self._conn.commit()

    def try_record_outbound(self, customer_id: str, action_id: str, now: float, window_seconds: float = 60.0) -> bool:
        with self._lock:
            # IMMEDIATE makes cleanup + check + insert one SQLite transaction.
            # It serializes competing writers even when callers use separate processes.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cutoff = now - window_seconds
                self._conn.execute("DELETE FROM outbound_messages WHERE customer_id=? AND sent_at < ?", (customer_id, cutoff))
                row = self._conn.execute("SELECT 1 FROM outbound_messages WHERE customer_id=? LIMIT 1", (customer_id,)).fetchone()
                if row:
                    self._conn.commit()
                    return False
                self._conn.execute("INSERT INTO outbound_messages VALUES (?, ?, ?)", (action_id, customer_id, now))
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def release_outbound_reservation(self, action_id: str) -> None:
        """Release a rate-limit reservation when no provider delivery was accepted."""
        with self._lock:
            self._conn.execute("DELETE FROM outbound_messages WHERE action_id=?", (action_id,))
            self._conn.commit()

    def find_event_for_message(self, customer_id: str, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE customer_id=? AND message_id=? ORDER BY created_at DESC LIMIT 1", (customer_id, message_id)).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()
