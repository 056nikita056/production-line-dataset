from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    total_items INTEGER NOT NULL DEFAULT 0,
                    processed_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    review_items INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    original_name TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    result_path TEXT,
                    preview_path TEXT,
                    raw_response_path TEXT,
                    validation_path TEXT,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    review_reasons TEXT NOT NULL DEFAULT '[]',
                    validation_errors TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    imported_legacy INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    approved_at TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
                CREATE INDEX IF NOT EXISTS idx_items_run ON items(run_id);

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    exit_code INTEGER,
                    duration_ms INTEGER NOT NULL,
                    timed_out INTEGER NOT NULL DEFAULT 0,
                    stdout TEXT NOT NULL DEFAULT '',
                    stderr TEXT NOT NULL DEFAULT '',
                    error_message TEXT,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );

                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY,
                    zip_path TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    class_stats TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    reference_id TEXT,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def fetch_one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, parameters).fetchone()
        return dict(row) if row else None

    def fetch_all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [dict(row) for row in rows]

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, parameters)
            return cursor.rowcount

    def record_error(
        self,
        scope: str,
        code: str,
        message: str,
        reference_id: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO errors(scope, reference_id, code, message, created_at) VALUES(?,?,?,?,?)",
            (scope, reference_id, code, message[:2000], utc_now()),
        )

    def recent_errors(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.fetch_all(
            "SELECT scope, reference_id, code, message, created_at "
            "FROM errors ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    @staticmethod
    def json_value(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

