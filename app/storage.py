from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .domain import Session, SessionStatus


class SQLiteStore:
    def __init__(self, path: str = "data/agent.db") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions (
              customer_id TEXT PRIMARY KEY, status TEXT NOT NULL,
              abnormal_streak INTEGER NOT NULL, version INTEGER NOT NULL,
              last_outbound_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
              message_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
              direction TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
              message_id TEXT, action_type TEXT NOT NULL, result TEXT NOT NULL,
              reason TEXT NOT NULL, trace_id TEXT NOT NULL, payload TEXT, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbound_messages (
              action_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, sent_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_session(self, customer_id: str) -> Session:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sessions WHERE customer_id = ?", (customer_id,)).fetchone()
            if row:
                values = dict(row)
                values["status"] = SessionStatus(values["status"])
                return Session(**values)
            now = time.time()
            session = Session(customer_id=customer_id, created_at=now, updated_at=now)
            self._conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (customer_id, session.status.value, 0, 0, None, now, now),
            )
            self._conn.commit()
            return session

    def save_session(self, session: Session) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status=?, abnormal_streak=?, version=?, last_outbound_at=?, updated_at=? WHERE customer_id=?",
                (session.status.value, session.abnormal_streak, session.version, session.last_outbound_at, session.updated_at, session.customer_id),
            )
            self._conn.commit()

    def add_message(self, message_id: str, customer_id: str, direction: str, content: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", (message_id, customer_id, direction, content, time.time()))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def add_event(self, action_id: str, customer_id: str, message_id: str | None, action_type: str, result: str, reason: str, trace_id: str, payload: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (action_id, customer_id, message_id, action_type, result, reason, trace_id, json.dumps(payload or {}), time.time()))
            self._conn.commit()

    def try_record_outbound(self, customer_id: str, action_id: str, now: float, window_seconds: float = 60.0) -> bool:
        with self._lock:
            cutoff = now - window_seconds
            self._conn.execute("DELETE FROM outbound_messages WHERE customer_id=? AND sent_at < ?", (customer_id, cutoff))
            row = self._conn.execute("SELECT 1 FROM outbound_messages WHERE customer_id=? LIMIT 1", (customer_id,)).fetchone()
            if row:
                self._conn.commit()
                return False
            self._conn.execute("INSERT INTO outbound_messages VALUES (?, ?, ?)", (action_id, customer_id, now))
            self._conn.commit()
            return True

    def find_event_for_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE message_id=? ORDER BY created_at DESC LIMIT 1", (message_id,)).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()
