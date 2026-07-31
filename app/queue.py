from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Database, utc_now
from .models import ItemStatus, RunStatus


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ItemStatus.PENDING: {ItemStatus.PROCESSING},
    ItemStatus.PROCESSING: {ItemStatus.REVIEW, ItemStatus.FAILED},
    ItemStatus.REVIEW: {ItemStatus.APPROVED, ItemStatus.REJECTED, ItemStatus.PROCESSING},
    ItemStatus.FAILED: {ItemStatus.PENDING},
    ItemStatus.REJECTED: {ItemStatus.PROCESSING},
    ItemStatus.APPROVED: set(),
}


class QueueError(RuntimeError):
    pass


class ItemNotFound(QueueError):
    pass


class InvalidTransition(QueueError):
    pass


class QueueRepository:
    def __init__(self, db: Database):
        self.db = db

    def add_item(
        self,
        *,
        item_id: str,
        sha256: str,
        original_name: str,
        source_path: str,
        width: int,
        height: int,
        imported_legacy: bool = False,
        status: str = ItemStatus.PENDING,
    ) -> bool:
        now = utc_now()
        with self.db.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO items(
                        id, sha256, original_name, source_path, status, width, height,
                        imported_legacy, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        item_id,
                        sha256,
                        original_name,
                        source_path,
                        str(status),
                        width,
                        height,
                        int(imported_legacy),
                        now,
                        now,
                    ),
                )
                return True
            except Exception as exc:
                if "UNIQUE constraint failed: items.sha256" in str(exc):
                    return False
                raise

    def create_run(self) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT id FROM items WHERE status=? AND run_id IS NULL ORDER BY created_at, id",
                (ItemStatus.PENDING,),
            ).fetchall()
            conn.execute(
                "INSERT INTO runs(id,status,created_at,total_items) VALUES(?,?,?,?)",
                (run_id, RunStatus.CREATED, now, len(pending)),
            )
            if pending:
                conn.executemany(
                    "UPDATE items SET run_id=?, updated_at=? WHERE id=?",
                    [(run_id, now, row["id"]) for row in pending],
                )
            conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.db.fetch_one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not run:
            raise QueueError("Запуск не найден")
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.fetch_all("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))

    def set_run_status(self, run_id: str, status: str) -> None:
        now = utc_now()
        started = now if status == RunStatus.PROCESSING else None
        finished = now if status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_REVIEW,
            RunStatus.FAILED,
        } else None
        self.db.execute(
            """
            UPDATE runs
            SET status=?,
                started_at=COALESCE(started_at, ?),
                finished_at=COALESCE(?, finished_at)
            WHERE id=?
            """,
            (str(status), started, finished, run_id),
        )

    def finalize_run(self, run_id: str) -> dict[str, Any]:
        counts = self.db.fetch_all(
            "SELECT status, COUNT(*) AS count FROM items WHERE run_id=? GROUP BY status",
            (run_id,),
        )
        by_status = {row["status"]: row["count"] for row in counts}
        total = sum(by_status.values())
        failed = by_status.get(ItemStatus.FAILED, 0)
        review = by_status.get(ItemStatus.REVIEW, 0)
        processing = by_status.get(ItemStatus.PROCESSING, 0) + by_status.get(ItemStatus.PENDING, 0)
        if processing:
            status = RunStatus.PROCESSING
        elif failed == total and total > 0:
            status = RunStatus.FAILED
        elif review or failed:
            status = RunStatus.COMPLETED_WITH_REVIEW
        else:
            status = RunStatus.COMPLETED
        finished = utc_now() if status != RunStatus.PROCESSING else None
        self.db.execute(
            """
            UPDATE runs SET status=?, processed_items=?, failed_items=?, review_items=?,
                finished_at=? WHERE id=?
            """,
            (status, total - failed, failed, review, finished, run_id),
        )
        return self.get_run(run_id)

    def claim_next(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM items
                WHERE run_id=? AND status=?
                ORDER BY created_at, id LIMIT 1
                """,
                (run_id, ItemStatus.PENDING),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            changed = conn.execute(
                "UPDATE items SET status=?, updated_at=? WHERE id=? AND status=?",
                (ItemStatus.PROCESSING, utc_now(), row["id"], ItemStatus.PENDING),
            ).rowcount
            conn.commit()
        return self.get_item(row["id"]) if changed == 1 else None

    def get_item(self, item_id: str) -> dict[str, Any]:
        item = self.db.fetch_one("SELECT * FROM items WHERE id=?", (item_id,))
        if not item:
            raise ItemNotFound("Кадр не найден")
        for key in ("review_reasons", "validation_errors"):
            try:
                item[key] = json.loads(item[key] or "[]")
            except json.JSONDecodeError:
                item[key] = []
        return item

    @staticmethod
    def _decode_revision(revision: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "annotation_json",
            "validation_errors",
            "validation_warnings",
            "review_reasons",
        ):
            fallback: dict[str, Any] | list[Any] = {} if key == "annotation_json" else []
            try:
                encoded = (
                    revision[key] or "{}"
                    if key == "annotation_json"
                    else revision[key] or "[]"
                )
                revision[key] = json.loads(encoded)
            except (TypeError, json.JSONDecodeError):
                revision[key] = fallback
        revision["annotation"] = revision.pop("annotation_json")
        return revision

    def get_revision(self, item_id: str, revision_id: str) -> dict[str, Any]:
        revision = self.db.fetch_one(
            """
            SELECT * FROM annotation_revisions
            WHERE id=? AND item_id=?
            """,
            (revision_id, item_id),
        )
        if not revision:
            raise QueueError("Ревизия разметки не найдена")
        return self._decode_revision(revision)

    def list_revisions(self, item_id: str) -> list[dict[str, Any]]:
        self.get_item(item_id)
        revisions = self.db.fetch_all(
            """
            SELECT * FROM annotation_revisions
            WHERE item_id=?
            ORDER BY revision_no DESC
            """,
            (item_id,),
        )
        return [self._decode_revision(revision) for revision in revisions]

    def next_revision_number(self, item_id: str) -> int:
        self.get_item(item_id)
        row = self.db.fetch_one(
            """
            SELECT COALESCE(MAX(revision_no), 0) + 1 AS revision_no
            FROM annotation_revisions
            WHERE item_id=?
            """,
            (item_id,),
        )
        return int(row["revision_no"]) if row else 1

    def select_revision(self, item_id: str, revision_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] != ItemStatus.REVIEW:
            raise InvalidTransition(
                "Выбирать ревизию можно только у кадра на проверке"
            )
        revision = self.get_revision(item_id, revision_id)
        changed = self.db.execute(
            """
            UPDATE items
            SET selected_revision_id=?, last_annotation_source=?,
                result_path=?, preview_path=?, raw_response_path=?,
                validation_path=?, review_reasons=?, validation_errors=?,
                error_message=?, updated_at=?
            WHERE id=? AND status=?
            """,
            (
                revision_id,
                revision["source"],
                revision["label_path"],
                revision["preview_path"],
                revision["raw_response_path"],
                revision["validation_path"],
                self.db.json_value(revision["review_reasons"]),
                self.db.json_value(revision["validation_errors"]),
                (
                    "Требуется исправить ошибки валидации"
                    if revision["validation_errors"]
                    else None
                ),
                utc_now(),
                item_id,
                ItemStatus.REVIEW,
            ),
        )
        if changed != 1:
            raise InvalidTransition("Не удалось выбрать ревизию")
        return self.get_item(item_id)

    def create_revision(
        self,
        item_id: str,
        *,
        annotation: dict[str, Any],
        source: str,
        attempt_id: int | None,
        result_path: str | None,
        preview_path: str | None,
        raw_response_path: str | None,
        validation_path: str | None,
        review_reasons: list[str],
        validation_errors: list[str],
        validation_warnings: list[str],
        error_message: str | None,
        transition_to_review: bool,
    ) -> dict[str, Any]:
        revision_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                item = conn.execute(
                    "SELECT status FROM items WHERE id=?",
                    (item_id,),
                ).fetchone()
                if not item:
                    raise ItemNotFound("Кадр не найден")
                allowed = {ItemStatus.PROCESSING, ItemStatus.REVIEW}
                if item["status"] not in allowed:
                    raise InvalidTransition(
                        "Создать ревизию можно только при обработке или проверке"
                    )
                if transition_to_review and item["status"] != ItemStatus.PROCESSING:
                    raise InvalidTransition(
                        "Завершить обработку можно только из статуса processing"
                    )
                row = conn.execute(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1 AS revision_no
                    FROM annotation_revisions
                    WHERE item_id=?
                    """,
                    (item_id,),
                ).fetchone()
                revision_no = int(row["revision_no"])
                conn.execute(
                    """
                    INSERT INTO annotation_revisions(
                        id, item_id, attempt_id, revision_no, source,
                        annotation_json, validation_errors, validation_warnings,
                        review_reasons, preview_path, label_path,
                        raw_response_path, validation_path, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        revision_id,
                        item_id,
                        attempt_id,
                        revision_no,
                        source,
                        self.db.json_value(annotation),
                        self.db.json_value(validation_errors),
                        self.db.json_value(validation_warnings),
                        self.db.json_value(review_reasons),
                        preview_path,
                        result_path,
                        raw_response_path,
                        validation_path,
                        now,
                    ),
                )
                status = (
                    ItemStatus.REVIEW
                    if transition_to_review
                    else item["status"]
                )
                conn.execute(
                    """
                    UPDATE items
                    SET status=?, selected_revision_id=?,
                        last_annotation_source=?, result_path=?, preview_path=?,
                        raw_response_path=?, validation_path=?,
                        review_reasons=?, validation_errors=?,
                        error_message=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        status,
                        revision_id,
                        source,
                        result_path,
                        preview_path,
                        raw_response_path,
                        validation_path,
                        self.db.json_value(review_reasons),
                        self.db.json_value(validation_errors),
                        error_message,
                        now,
                        item_id,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_item(item_id)

    def list_items(
        self,
        *,
        status: str | None = None,
        run_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if run_id:
            clauses.append("run_id=?")
            values.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetch_all(
            f"SELECT * FROM items {where} ORDER BY created_at DESC, id LIMIT ?",
            tuple(values + [limit]),
        )
        return [self.get_item(row["id"]) for row in rows]

    def next_review_item(self, item_id: str) -> dict[str, Any] | None:
        current = self.get_item(item_id)
        parameters = (
            ItemStatus.REVIEW,
            item_id,
            current["created_at"],
            current["created_at"],
            item_id,
        )
        row = self.db.fetch_one(
            """
            SELECT id FROM items
            WHERE status=? AND id<>?
              AND (created_at < ? OR (created_at = ? AND id > ?))
            ORDER BY created_at DESC, id
            LIMIT 1
            """,
            parameters,
        )
        if not row:
            row = self.db.fetch_one(
                """
                SELECT id FROM items
                WHERE status=? AND id<>?
                ORDER BY created_at DESC, id
                LIMIT 1
                """,
                (ItemStatus.REVIEW, item_id),
            )
        return self.get_item(row["id"]) if row else None

    def transition(self, item_id: str, target: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                conn.rollback()
                raise ItemNotFound("Кадр не найден")
            source = row["status"]
            if target not in ALLOWED_TRANSITIONS.get(source, set()):
                conn.rollback()
                raise InvalidTransition(f"Недопустимый переход {source} -> {target}")
            approved_at = utc_now() if target == ItemStatus.APPROVED else None
            conn.execute(
                "UPDATE items SET status=?, updated_at=?, approved_at=? WHERE id=?",
                (str(target), utc_now(), approved_at, item_id),
            )
            conn.commit()
        return self.get_item(item_id)

    def retry(self, item_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] == ItemStatus.FAILED:
            retried = self.transition(item_id, ItemStatus.PENDING)
            self.db.execute(
                "UPDATE items SET run_id=NULL, error_message=NULL, updated_at=? WHERE id=?",
                (utc_now(), item_id),
            )
            return self.get_item(item_id)
        if item["status"] in {ItemStatus.REVIEW, ItemStatus.REJECTED}:
            return self.transition(item_id, ItemStatus.PROCESSING)
        raise InvalidTransition(f"Кадр в статусе {item['status']} нельзя повторить")

    def approve(self, item_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["validation_errors"]:
            raise QueueError("Нельзя принять результат с ошибками валидации")
        revision_id = item.get("selected_revision_id")
        if not revision_id:
            raise QueueError("Нельзя принять результат без выбранной ревизии")
        revision = self.get_revision(item_id, revision_id)
        if revision["validation_errors"]:
            raise QueueError("Нельзя принять ревизию с ошибками валидации")
        if not revision["label_path"] or not revision["preview_path"]:
            raise QueueError("Нельзя принять результат без label и preview")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE items
                SET status=?, approved_revision_id=?, approved_at=?, updated_at=?
                WHERE id=? AND status=? AND selected_revision_id=?
                """,
                (
                    ItemStatus.APPROVED,
                    revision_id,
                    utc_now(),
                    utc_now(),
                    item_id,
                    ItemStatus.REVIEW,
                    revision_id,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise InvalidTransition("Принять можно только кадр на проверке")
            conn.commit()
        return self.get_item(item_id)

    def reject(self, item_id: str) -> dict[str, Any]:
        return self.transition(item_id, ItemStatus.REJECTED)

    def complete_processing(
        self,
        item_id: str,
        *,
        result_path: str | None,
        preview_path: str | None,
        raw_response_path: str | None,
        validation_path: str | None,
        review_reasons: list[str],
        validation_errors: list[str],
        error_message: str | None = None,
        failed: bool = False,
        annotation: dict[str, Any] | None = None,
        source: str = "automatic",
        attempt_id: int | None = None,
        validation_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] != ItemStatus.PROCESSING:
            raise InvalidTransition(f"Ожидался processing, получен {item['status']}")
        if not failed:
            return self.create_revision(
                item_id,
                annotation=annotation or {},
                source=source,
                attempt_id=attempt_id,
                result_path=result_path,
                preview_path=preview_path,
                raw_response_path=raw_response_path,
                validation_path=validation_path,
                review_reasons=review_reasons,
                validation_errors=validation_errors,
                validation_warnings=validation_warnings or [],
                error_message=error_message,
                transition_to_review=True,
            )
        self.db.execute(
            """
            UPDATE items SET status=?, result_path=?, preview_path=?, raw_response_path=?,
                validation_path=?, review_reasons=?, validation_errors=?, error_message=?,
                updated_at=? WHERE id=? AND status=?
            """,
            (
                ItemStatus.FAILED,
                result_path,
                preview_path,
                raw_response_path,
                validation_path,
                self.db.json_value(review_reasons),
                self.db.json_value(validation_errors),
                error_message,
                utc_now(),
                item_id,
                ItemStatus.PROCESSING,
            ),
        )
        return self.get_item(item_id)

    def recover_processing(self) -> int:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id FROM items WHERE status=?", (ItemStatus.PROCESSING,)
            ).fetchall()
            for row in rows:
                conn.execute(
                    """
                    UPDATE items SET status=?, run_id=NULL, updated_at=?,
                        error_message=? WHERE id=?
                    """,
                    (
                        ItemStatus.PENDING,
                        now,
                        "Обработка была прервана; задание безопасно возвращено в очередь",
                        row["id"],
                    ),
                )
            conn.commit()
        return len(rows)

    def add_attempt(
        self,
        item_id: str,
        attempt_no: int,
        *,
        started_at: str,
        finished_at: str,
        exit_code: int | None,
        duration_ms: int,
        timed_out: bool,
        stdout: str,
        stderr: str,
        error_message: str | None,
    ) -> int:
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO attempts(
                    item_id, attempt_no, started_at, finished_at, exit_code, duration_ms,
                    timed_out, stdout, stderr, error_message
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_id,
                    attempt_no,
                    started_at,
                    finished_at,
                    exit_code,
                    duration_ms,
                    int(timed_out),
                    stdout[-20_000:],
                    stderr[-20_000:],
                    error_message,
                ),
            )
            return int(cursor.lastrowid)

    def status_counts(self) -> dict[str, int]:
        rows = self.db.fetch_all("SELECT status, COUNT(*) AS count FROM items GROUP BY status")
        counts = {status.value: 0 for status in ItemStatus}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts
