from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MigrationHandler = Callable[[sqlite3.Connection, Path], None]


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    apply: MigrationHandler
    requires_backup: bool = False


def _create_initial_schema(conn: sqlite3.Connection, _root: Path) -> None:
    statements = (
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            total_items INTEGER NOT NULL DEFAULT 0,
            processed_items INTEGER NOT NULL DEFAULT 0,
            failed_items INTEGER NOT NULL DEFAULT 0,
            review_items INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE items (
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
        )
        """,
        "CREATE INDEX idx_items_status ON items(status)",
        "CREATE INDEX idx_items_run ON items(run_id)",
        """
        CREATE TABLE attempts (
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
        )
        """,
        """
        CREATE TABLE exports (
            id TEXT PRIMARY KEY,
            zip_path TEXT NOT NULL,
            item_count INTEGER NOT NULL,
            class_stats TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            reference_id TEXT,
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
    )
    for statement in statements:
        conn.execute(statement)


def _read_json_file(root: Path, relative_path: str | None) -> object | None:
    if not relative_path:
        return None
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _json_list(value: str | None) -> list[str]:
    try:
        decoded = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(entry) for entry in decoded] if isinstance(decoded, list) else []


def _add_annotation_revisions(conn: sqlite3.Connection, root: Path) -> None:
    conn.execute("ALTER TABLE items ADD COLUMN selected_revision_id TEXT")
    conn.execute("ALTER TABLE items ADD COLUMN approved_revision_id TEXT")
    conn.execute("ALTER TABLE items ADD COLUMN last_annotation_source TEXT")
    conn.execute(
        """
        CREATE TABLE annotation_revisions (
            id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            attempt_id INTEGER,
            revision_no INTEGER NOT NULL,
            source TEXT NOT NULL,
            annotation_json TEXT NOT NULL,
            validation_errors TEXT NOT NULL DEFAULT '[]',
            validation_warnings TEXT NOT NULL DEFAULT '[]',
            review_reasons TEXT NOT NULL DEFAULT '[]',
            preview_path TEXT,
            label_path TEXT,
            raw_response_path TEXT,
            validation_path TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(item_id) REFERENCES items(id),
            FOREIGN KEY(attempt_id) REFERENCES attempts(id),
            UNIQUE(item_id, revision_no)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_annotation_revisions_item
        ON annotation_revisions(item_id, revision_no)
        """
    )

    rows = conn.execute(
        """
        SELECT id, status, imported_legacy, result_path, preview_path,
               raw_response_path, validation_path, review_reasons,
               validation_errors, updated_at, created_at
        FROM items
        WHERE result_path IS NOT NULL
           OR preview_path IS NOT NULL
           OR raw_response_path IS NOT NULL
        ORDER BY created_at, id
        """
    ).fetchall()
    for row in rows:
        raw_annotation = _read_json_file(root, row["raw_response_path"])
        validation_report = _read_json_file(root, row["validation_path"])
        validation_warnings: list[str] = []
        if isinstance(validation_report, dict):
            warnings = validation_report.get("warnings", [])
            if isinstance(warnings, list):
                validation_warnings = [str(entry) for entry in warnings]
        validation_errors = _json_list(row["validation_errors"])
        if raw_annotation is None:
            raw_annotation = {}
            if "legacy_annotation_unreadable" not in validation_errors:
                validation_errors.append("legacy_annotation_unreadable")
        attempt = conn.execute(
            """
            SELECT id FROM attempts
            WHERE item_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (row["id"],),
        ).fetchone()
        revision_id = str(uuid.uuid4())
        source = "legacy_import" if row["imported_legacy"] else "automatic"
        conn.execute(
            """
            INSERT INTO annotation_revisions(
                id, item_id, attempt_id, revision_no, source, annotation_json,
                validation_errors, validation_warnings, review_reasons,
                preview_path, label_path, raw_response_path, validation_path,
                created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                revision_id,
                row["id"],
                attempt["id"] if attempt else None,
                1,
                source,
                json.dumps(raw_annotation, ensure_ascii=False, separators=(",", ":")),
                json.dumps(validation_errors, ensure_ascii=False, separators=(",", ":")),
                json.dumps(validation_warnings, ensure_ascii=False, separators=(",", ":")),
                json.dumps(
                    _json_list(row["review_reasons"]),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                row["preview_path"],
                row["result_path"],
                row["raw_response_path"],
                row["validation_path"],
                row["updated_at"] or row["created_at"],
            ),
        )
        conn.execute(
            """
            UPDATE items
            SET selected_revision_id=?,
                approved_revision_id=CASE WHEN status='approved' THEN ? ELSE NULL END,
                last_annotation_source=?
            WHERE id=?
            """,
            (revision_id, revision_id, source, row["id"]),
        )


def _add_recognition_cycles(conn: sqlite3.Connection, _root: Path) -> None:
    conn.execute(
        """
        ALTER TABLE runs
        ADD COLUMN recognition_mode TEXT NOT NULL DEFAULT 'single'
        """
    )
    conn.execute(
        """
        ALTER TABLE runs
        ADD COLUMN max_auto_attempts INTEGER NOT NULL DEFAULT 1
        """
    )
    conn.execute(
        """
        ALTER TABLE runs
        ADD COLUMN codex_call_count INTEGER NOT NULL DEFAULT 0
        """
    )
    conn.execute(
        """
        ALTER TABLE runs
        ADD COLUMN total_duration_ms INTEGER NOT NULL DEFAULT 0
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN run_id TEXT
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN cycle_no INTEGER NOT NULL DEFAULT 1
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN quality_attempt_no INTEGER NOT NULL DEFAULT 1
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN attempt_kind TEXT NOT NULL DEFAULT 'initial'
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN trigger_reason TEXT NOT NULL DEFAULT 'initial'
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN image_count INTEGER NOT NULL DEFAULT 1
        """
    )
    conn.execute("ALTER TABLE attempts ADD COLUMN raw_response_path TEXT")
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN validation_errors TEXT NOT NULL DEFAULT '[]'
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN validation_warnings TEXT NOT NULL DEFAULT '[]'
        """
    )
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN review_reasons TEXT NOT NULL DEFAULT '[]'
        """
    )
    conn.execute("ALTER TABLE attempts ADD COLUMN annotation_json TEXT")
    conn.execute("ALTER TABLE attempts ADD COLUMN preview_path TEXT")
    conn.execute("ALTER TABLE attempts ADD COLUMN label_path TEXT")
    conn.execute("ALTER TABLE attempts ADD COLUMN validation_path TEXT")
    conn.execute(
        """
        ALTER TABLE attempts
        ADD COLUMN selected INTEGER NOT NULL DEFAULT 0
        """
    )
    conn.execute(
        """
        UPDATE attempts
        SET run_id=(
            SELECT items.run_id FROM items WHERE items.id=attempts.item_id
        )
        """
    )
    conn.execute(
        """
        UPDATE attempts
        SET raw_response_path=(
                SELECT revision.raw_response_path
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ),
            validation_errors=COALESCE((
                SELECT revision.validation_errors
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ), '[]'),
            validation_warnings=COALESCE((
                SELECT revision.validation_warnings
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ), '[]'),
            review_reasons=COALESCE((
                SELECT revision.review_reasons
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ), '[]'),
            annotation_json=(
                SELECT revision.annotation_json
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ),
            preview_path=(
                SELECT revision.preview_path
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ),
            label_path=(
                SELECT revision.label_path
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ),
            validation_path=(
                SELECT revision.validation_path
                FROM annotation_revisions AS revision
                WHERE revision.attempt_id=attempts.id
                LIMIT 1
            ),
            selected=CASE WHEN EXISTS(
                SELECT 1
                FROM annotation_revisions AS revision
                JOIN items ON items.selected_revision_id=revision.id
                WHERE revision.attempt_id=attempts.id
            ) THEN 1 ELSE 0 END
        """
    )
    conn.execute(
        """
        UPDATE runs
        SET codex_call_count=(
                SELECT COUNT(*) FROM attempts
                WHERE attempts.run_id=runs.id
            ),
            total_duration_ms=COALESCE((
                SELECT SUM(duration_ms) FROM attempts
                WHERE attempts.run_id=runs.id
            ), 0)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_attempts_item_cycle
        ON attempts(item_id, cycle_no, quality_attempt_no)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_attempts_run
        ON attempts(run_id)
        """
    )


def _add_attempt_selection_reason(
    conn: sqlite3.Connection,
    _root: Path,
) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(attempts)").fetchall()
    }
    if "selection_reason" not in columns:
        conn.execute("ALTER TABLE attempts ADD COLUMN selection_reason TEXT")


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "initial_schema", _create_initial_schema),
    Migration(
        2,
        "annotation_revisions",
        _add_annotation_revisions,
        requires_backup=True,
    ),
    Migration(
        3,
        "recognition_cycles",
        _add_recognition_cycles,
        requires_backup=True,
    ),
    Migration(
        4,
        "attempt_selection_reason",
        _add_attempt_selection_reason,
        requires_backup=True,
    ),
)

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version
