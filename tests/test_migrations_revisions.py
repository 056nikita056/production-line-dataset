from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, Migration


def _create_legacy_database(root: Path, payload: dict[str, object]) -> Path:
    database_path = root / "queue.sqlite3"
    raw_path = root / "data" / "raw.json"
    validation_path = root / "data" / "validation.json"
    preview_path = root / "data" / "preview.jpg"
    label_path = root / "data" / "label.txt"
    raw_path.parent.mkdir(parents=True)
    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    validation_path.write_text(
        json.dumps({"valid": True, "errors": [], "warnings": ["legacy_warning"]}),
        encoding="utf-8",
    )
    preview_path.write_bytes(b"preview")
    label_path.write_text("0 0.1 0.1 0.2 0.2 0.1 0.2\n", encoding="utf-8")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        MIGRATIONS[0].apply(connection, root)
        connection.execute(
            """
            INSERT INTO items(
                id, sha256, original_name, source_path, status, run_id,
                result_path, preview_path, raw_response_path, validation_path,
                width, height, review_reasons, validation_errors,
                error_message, imported_legacy, created_at, updated_at,
                approved_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-item",
                "a" * 64,
                "legacy.jpg",
                "data/source.jpg",
                "approved",
                None,
                "data/label.txt",
                "data/preview.jpg",
                "data/raw.json",
                "data/validation.json",
                160,
                90,
                "[]",
                "[]",
                None,
                0,
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO attempts(
                item_id, attempt_no, started_at, finished_at, exit_code,
                duration_ms, timed_out, stdout, stderr, error_message
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "legacy-item",
                1,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:01+00:00",
                0,
                1000,
                0,
                "",
                "",
                None,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return database_path


def test_new_database_is_created_at_current_schema(tmp_path):
    database = Database(tmp_path / "queue.sqlite3")
    database.initialize()

    assert database.schema_version() == CURRENT_SCHEMA_VERSION
    assert database.last_backup_path is None
    tables = {
        row["name"]
        for row in database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "schema_version",
        "items",
        "annotation_revisions",
        "detail_regions",
    } <= tables


def test_legacy_database_migrates_with_readable_backup(
    tmp_path,
    valid_payload,
):
    database_path = _create_legacy_database(tmp_path, valid_payload)
    database = Database(database_path)
    before = database.fetch_all(
        "SELECT id, status FROM items ORDER BY id"
    )

    database.initialize()

    after = database.fetch_all("SELECT id, status FROM items ORDER BY id")
    item = database.fetch_one(
        """
        SELECT status, selected_revision_id, approved_revision_id
        FROM items WHERE id='legacy-item'
        """
    )
    revisions = database.fetch_all(
        "SELECT * FROM annotation_revisions WHERE item_id='legacy-item'"
    )
    assert after == before
    assert item["status"] == "approved"
    assert item["selected_revision_id"] == item["approved_revision_id"]
    assert len(revisions) == 1
    assert revisions[0]["attempt_id"] == 1
    assert revisions[0]["is_draft"] == 0
    assert revisions[0]["updated_at"] == revisions[0]["created_at"]
    assert json.loads(revisions[0]["annotation_json"]) == valid_payload
    assert json.loads(revisions[0]["validation_warnings"]) == [
        "legacy_warning"
    ]

    backup = database.last_backup_path
    assert backup and backup.is_file()
    backup_connection = sqlite3.connect(backup)
    try:
        assert backup_connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup_connection.execute(
            "SELECT COUNT(*) FROM items"
        ).fetchone()[0] == 1
        assert backup_connection.execute(
            "SELECT status FROM items WHERE id='legacy-item'"
        ).fetchone()[0] == "approved"
    finally:
        backup_connection.close()

    database.initialize()
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM annotation_revisions"
    )["count"] == 1


def test_failed_migration_rolls_back_all_schema_changes(tmp_path):
    database = Database(tmp_path / "queue.sqlite3")
    database.initialize()

    def fail_after_schema_change(
        connection: sqlite3.Connection,
        _root: Path,
    ) -> None:
        connection.execute("CREATE TABLE partial_change(id INTEGER)")
        raise RuntimeError("test failure")

    migration = Migration(
        CURRENT_SCHEMA_VERSION + 1,
        "intentional_failure",
        fail_after_schema_change,
    )
    with pytest.raises(RuntimeError, match="test failure"):
        database.apply_migration(migration)

    assert database.fetch_one(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='partial_change'
        """
    ) is None
    assert database.schema_version() == CURRENT_SCHEMA_VERSION
