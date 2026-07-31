from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, Migration


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.last_backup_path: Path | None = None

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            tables = {
                row["name"]
                for row in conn.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name NOT LIKE 'sqlite_%'
                    """
                ).fetchall()
            }

        has_application_tables = bool(tables - {"schema_version"})
        legacy_database = bool(has_application_tables and "schema_version" not in tables)
        if legacy_database:
            self._mark_legacy_baseline()

        current_version = self.schema_version()
        if current_version > CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                "База создана более новой версией приложения "
                f"({current_version} > {CURRENT_SCHEMA_VERSION})"
            )
        pending = [
            migration
            for migration in MIGRATIONS
            if migration.version > current_version
        ]
        if (
            has_application_tables
            and any(migration.requires_backup for migration in pending)
        ):
            self.last_backup_path = self.create_backup(
                before_version=pending[-1].version
            )
        for migration in pending:
            self.apply_migration(migration)

    def _mark_legacy_baseline(self) -> None:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE schema_version (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO schema_version(version, name, applied_at)
                    VALUES(1, 'initial_schema_legacy', ?)
                    """,
                    (utc_now(),),
                )
                conn.execute("PRAGMA user_version = 1")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def schema_version(self) -> int:
        with self.connect() as conn:
            exists = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='schema_version'
                """
            ).fetchone()
            if not exists:
                return 0
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version FROM schema_version"
            ).fetchone()
            return int(row["version"])

    def apply_migration(self, migration: Migration) -> None:
        with self.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_version (
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                already_applied = conn.execute(
                    "SELECT 1 FROM schema_version WHERE version=?",
                    (migration.version,),
                ).fetchone()
                if already_applied:
                    conn.commit()
                    return
                migration.apply(conn, self.path.parent)
                conn.execute(
                    """
                    INSERT INTO schema_version(version, name, applied_at)
                    VALUES(?,?,?)
                    """,
                    (migration.version, migration.name, utc_now()),
                )
                conn.execute(f"PRAGMA user_version = {migration.version}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def create_backup(self, *, before_version: int) -> Path:
        backup_dir = self.path.parent / f"{self.path.name}.backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = (
            backup_dir
            / f"{self.path.stem}.before-v{before_version}.{stamp}{self.path.suffix}"
        )
        with self.connect() as source:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
                check = destination.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RuntimeError("Не удалось проверить резервную копию базы")
            finally:
                destination.close()
        return backup_path

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
