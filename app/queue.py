from __future__ import annotations

import json
import uuid
from typing import Any

from .db import Database, utc_now
from .models import ItemStatus, RecognitionMode, RunStatus


ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ItemStatus.PENDING: {ItemStatus.PROCESSING},
    ItemStatus.PROCESSING: {ItemStatus.REVIEW, ItemStatus.FAILED},
    ItemStatus.REVIEW: {ItemStatus.APPROVED, ItemStatus.REJECTED, ItemStatus.PROCESSING},
    ItemStatus.FAILED: {ItemStatus.PENDING},
    ItemStatus.REJECTED: {ItemStatus.PROCESSING},
    ItemStatus.APPROVED: set(),
}

MANUAL_ANNOTATION_STATUSES = {
    ItemStatus.REVIEW,
    ItemStatus.REJECTED,
    ItemStatus.FAILED,
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

    def create_run(
        self,
        recognition_mode: str = RecognitionMode.SINGLE,
        *,
        detail_requested: bool = False,
    ) -> dict[str, Any]:
        mode = RecognitionMode(recognition_mode)
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT id FROM items WHERE status=? AND run_id IS NULL ORDER BY created_at, id",
                (ItemStatus.PENDING,),
            ).fetchall()
            conn.execute(
                """
                INSERT INTO runs(
                    id, status, created_at, total_items,
                    recognition_mode, max_auto_attempts, detail_requested
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    RunStatus.CREATED,
                    now,
                    len(pending),
                    mode,
                    mode.max_auto_attempts,
                    int(detail_requested),
                ),
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
        usage = self.db.fetch_one(
            """
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(duration_ms), 0) AS duration_ms
            FROM attempts
            WHERE run_id=?
            """,
            (run_id,),
        )
        self.db.execute(
            """
            UPDATE runs SET status=?, processed_items=?, failed_items=?, review_items=?,
                finished_at=?, codex_call_count=?, total_duration_ms=?
            WHERE id=?
            """,
            (
                status,
                total - failed,
                failed,
                review,
                finished,
                usage["calls"] if usage else 0,
                usage["duration_ms"] if usage else 0,
                run_id,
            ),
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
        if "is_draft" in revision:
            revision["is_draft"] = bool(revision["is_draft"])
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

    def create_manual_draft(
        self,
        item_id: str,
        annotation: dict[str, Any],
    ) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] not in MANUAL_ANNOTATION_STATUSES:
            raise InvalidTransition(
                "Ручную разметку можно открыть только для кадра на проверке"
            )
        existing = self.db.fetch_one(
            """
            SELECT id FROM annotation_revisions
            WHERE item_id=? AND is_draft=1
            ORDER BY updated_at DESC, revision_no DESC
            LIMIT 1
            """,
            (item_id,),
        )
        if existing:
            return self.get_revision(item_id, existing["id"])
        revision_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = conn.execute(
                    "SELECT status FROM items WHERE id=?",
                    (item_id,),
                ).fetchone()
                if not current:
                    raise ItemNotFound("Кадр не найден")
                if current["status"] not in MANUAL_ANNOTATION_STATUSES:
                    raise InvalidTransition(
                        "Ручную разметку можно открыть только для кадра на проверке"
                    )
                current_draft = conn.execute(
                    """
                    SELECT id FROM annotation_revisions
                    WHERE item_id=? AND is_draft=1
                    ORDER BY updated_at DESC, revision_no DESC
                    LIMIT 1
                    """,
                    (item_id,),
                ).fetchone()
                if current_draft:
                    conn.commit()
                    return self.get_revision(item_id, current_draft["id"])
                row = conn.execute(
                    """
                    SELECT COALESCE(MAX(revision_no), 0) + 1 AS revision_no
                    FROM annotation_revisions WHERE item_id=?
                    """,
                    (item_id,),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO annotation_revisions(
                        id, item_id, attempt_id, revision_no, source,
                        annotation_json, validation_errors, validation_warnings,
                        review_reasons, preview_path, label_path,
                        raw_response_path, validation_path, created_at,
                        is_draft, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        revision_id,
                        item_id,
                        None,
                        int(row["revision_no"]),
                        "manual_draft",
                        self.db.json_value(annotation),
                        "[]",
                        "[]",
                        "[]",
                        None,
                        None,
                        None,
                        None,
                        now,
                        1,
                        now,
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_revision(item_id, revision_id)

    def update_manual_draft(
        self,
        item_id: str,
        revision_id: str,
        annotation: dict[str, Any],
    ) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] not in MANUAL_ANNOTATION_STATUSES:
            raise InvalidTransition("Черновик нельзя менять в текущем статусе")
        changed = self.db.execute(
            """
            UPDATE annotation_revisions
            SET annotation_json=?, validation_errors='[]',
                validation_warnings='[]', review_reasons='[]',
                preview_path=NULL, label_path=NULL, raw_response_path=NULL,
                validation_path=NULL, updated_at=?
            WHERE id=? AND item_id=? AND is_draft=1
            """,
            (
                self.db.json_value(annotation),
                utc_now(),
                revision_id,
                item_id,
            ),
        )
        if changed != 1:
            raise QueueError("Черновик ручной разметки не найден")
        return self.get_revision(item_id, revision_id)

    def finalize_manual_draft(
        self,
        item_id: str,
        revision_id: str,
        *,
        annotation: dict[str, Any],
        result_path: str | None,
        preview_path: str,
        raw_response_path: str,
        validation_path: str,
        review_reasons: list[str],
        validation_errors: list[str],
        validation_warnings: list[str],
    ) -> dict[str, Any]:
        now = utc_now()
        valid = not validation_errors
        with self.db.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                item = conn.execute(
                    "SELECT status FROM items WHERE id=?",
                    (item_id,),
                ).fetchone()
                if not item:
                    raise ItemNotFound("Кадр не найден")
                if item["status"] not in MANUAL_ANNOTATION_STATUSES:
                    raise InvalidTransition(
                        "Ручную ревизию можно сохранить только на проверке"
                    )
                changed = conn.execute(
                    """
                    UPDATE annotation_revisions
                    SET source=?, annotation_json=?, validation_errors=?,
                        validation_warnings=?, review_reasons=?, preview_path=?,
                        label_path=?, raw_response_path=?, validation_path=?,
                        is_draft=?, updated_at=?
                    WHERE id=? AND item_id=? AND is_draft=1
                    """,
                    (
                        "manual" if valid else "manual_draft",
                        self.db.json_value(annotation),
                        self.db.json_value(validation_errors),
                        self.db.json_value(validation_warnings),
                        self.db.json_value(review_reasons),
                        preview_path,
                        result_path,
                        raw_response_path,
                        validation_path,
                        0 if valid else 1,
                        now,
                        revision_id,
                        item_id,
                    ),
                ).rowcount
                if changed != 1:
                    raise QueueError("Черновик ручной разметки не найден")
                if valid:
                    conn.execute(
                        """
                        UPDATE items
                        SET status=?, selected_revision_id=?, last_annotation_source='manual',
                            result_path=?, preview_path=?, raw_response_path=?,
                            validation_path=?, review_reasons=?, validation_errors='[]',
                            error_message=NULL, updated_at=?
                        WHERE id=? AND status=?
                        """,
                        (
                            ItemStatus.REVIEW,
                            revision_id,
                            result_path,
                            preview_path,
                            raw_response_path,
                            validation_path,
                            self.db.json_value(review_reasons),
                            now,
                            item_id,
                            item["status"],
                        ),
                    )
                    conn.execute(
                        """
                        UPDATE attempts
                        SET selected=0, selection_reason=NULL
                        WHERE item_id=?
                        """,
                        (item_id,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.get_revision(item_id, revision_id)

    def select_revision(self, item_id: str, revision_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] != ItemStatus.REVIEW:
            raise InvalidTransition(
                "Выбирать ревизию можно только у кадра на проверке"
            )
        revision = self.get_revision(item_id, revision_id)
        if revision.get("is_draft"):
            raise InvalidTransition("Черновик нужно сначала проверить и сохранить")
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
        self._mark_revision_attempt_selected(
            item_id,
            revision_id,
            selection_reason="manual_selection",
        )
        return self.get_item(item_id)

    def _mark_revision_attempt_selected(
        self,
        item_id: str,
        revision_id: str,
        *,
        selection_reason: str | None = None,
    ) -> None:
        revision = self.get_revision(item_id, revision_id)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE attempts
                SET selected=0, selection_reason=NULL
                WHERE item_id=?
                """,
                (item_id,),
            )
            if revision["attempt_id"] is not None:
                conn.execute(
                    """
                    UPDATE attempts
                    SET selected=1, selection_reason=?
                    WHERE id=? AND item_id=?
                    """,
                    (
                        selection_reason,
                        revision["attempt_id"],
                        item_id,
                    ),
                )
            conn.commit()

    def finalize_processing_revision(
        self,
        item_id: str,
        revision_id: str,
        *,
        selection_reason: str,
    ) -> dict[str, Any]:
        revision = self.get_revision(item_id, revision_id)
        review_reasons = list(revision["review_reasons"])
        if (
            selection_reason == "valid_results_disagree"
            and "attempts_disagree" not in review_reasons
        ):
            review_reasons.append("attempts_disagree")
        if (
            selection_reason == "detail_class_conflict"
            and "detail_class_conflict" not in review_reasons
        ):
            review_reasons.append("detail_class_conflict")
        changed = self.db.execute(
            """
            UPDATE items
            SET status=?, selected_revision_id=?, last_annotation_source=?,
                result_path=?, preview_path=?, raw_response_path=?,
                validation_path=?, review_reasons=?, validation_errors=?,
                error_message=?, updated_at=?
            WHERE id=? AND status=?
            """,
            (
                ItemStatus.REVIEW,
                revision_id,
                revision["source"],
                revision["label_path"],
                revision["preview_path"],
                revision["raw_response_path"],
                revision["validation_path"],
                self.db.json_value(review_reasons),
                self.db.json_value(revision["validation_errors"]),
                (
                    None
                    if not revision["validation_errors"]
                    else "Требуется исправить ошибки валидации"
                ),
                utc_now(),
                item_id,
                ItemStatus.PROCESSING,
            ),
        )
        if changed != 1:
            raise InvalidTransition(
                "Завершить выбор можно только из статуса processing"
            )
        self._mark_revision_attempt_selected(
            item_id,
            revision_id,
            selection_reason=selection_reason,
        )
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
                        raw_response_path, validation_path, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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

    def retry(
        self,
        item_id: str,
        recognition_mode: str = RecognitionMode.SINGLE,
        *,
        detail_requested: bool = False,
    ) -> dict[str, Any]:
        mode = RecognitionMode(recognition_mode)
        item = self.get_item(item_id)
        if item["status"] not in {
            ItemStatus.REVIEW,
            ItemStatus.REJECTED,
            ItemStatus.FAILED,
        }:
            raise InvalidTransition(
                f"Кадр в статусе {item['status']} нельзя повторить"
            )
        run_id = str(uuid.uuid4())
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO runs(
                    id, status, created_at, started_at, total_items,
                    recognition_mode, max_auto_attempts, detail_requested
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    RunStatus.PROCESSING,
                    now,
                    now,
                    1,
                    mode,
                    mode.max_auto_attempts,
                    int(detail_requested),
                ),
            )
            changed = conn.execute(
                """
                UPDATE items
                SET status=?, run_id=?, error_message=NULL, updated_at=?
                WHERE id=? AND status=?
                """,
                (
                    ItemStatus.PROCESSING,
                    run_id,
                    now,
                    item_id,
                    item["status"],
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise InvalidTransition("Статус кадра изменился")
            conn.commit()
        return self.get_item(item_id)

    def create_detail_region(
        self,
        item_id: str,
        *,
        left: int,
        top: int,
        right: int,
        bottom: int,
        reason: str = "manual_selection",
    ) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["status"] not in {
            ItemStatus.REVIEW,
            ItemStatus.REJECTED,
            ItemStatus.FAILED,
        }:
            raise InvalidTransition(
                "Область детализации можно выбрать только после обработки кадра"
            )
        if not (0 <= left < right <= item["width"]):
            raise ValueError("Некорректные горизонтальные границы области")
        if not (0 <= top < bottom <= item["height"]):
            raise ValueError("Некорректные вертикальные границы области")
        if right - left < 8 or bottom - top < 8:
            raise ValueError("Область должна быть не меньше 8 × 8 пикселей")
        count = self.db.fetch_one(
            """
            SELECT COUNT(*) AS count FROM detail_regions
            WHERE item_id=? AND attempt_id IS NULL
            """,
            (item_id,),
        )
        if count and count["count"] >= 4:
            raise QueueError("Можно выбрать не более четырёх областей")
        record_id = str(uuid.uuid4())
        region_id = f"manual-{record_id[:8]}"
        self.db.execute(
            """
            INSERT INTO detail_regions(
                id, item_id, attempt_id, region_id, left_px, top_px,
                right_px, bottom_px, crop_path, reason,
                target_object_index, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                record_id,
                item_id,
                None,
                region_id,
                left,
                top,
                right,
                bottom,
                None,
                reason,
                None,
                utc_now(),
            ),
        )
        return self.get_detail_region(item_id, record_id)

    def get_detail_region(
        self,
        item_id: str,
        region_record_id: str,
    ) -> dict[str, Any]:
        self.get_item(item_id)
        row = self.db.fetch_one(
            "SELECT * FROM detail_regions WHERE id=? AND item_id=?",
            (region_record_id, item_id),
        )
        if not row:
            raise QueueError("Область детализации не найдена")
        return row

    def list_detail_regions(
        self,
        item_id: str,
        *,
        pending_only: bool = False,
    ) -> list[dict[str, Any]]:
        self.get_item(item_id)
        pending = "AND attempt_id IS NULL" if pending_only else ""
        return self.db.fetch_all(
            f"""
            SELECT * FROM detail_regions
            WHERE item_id=? {pending}
            ORDER BY created_at, id
            """,
            (item_id,),
        )

    def delete_detail_region(
        self,
        item_id: str,
        region_record_id: str,
    ) -> None:
        self.get_item(item_id)
        changed = self.db.execute(
            """
            DELETE FROM detail_regions
            WHERE id=? AND item_id=? AND attempt_id IS NULL
            """,
            (region_record_id, item_id),
        )
        if changed != 1:
            raise QueueError("Выбранная область не найдена или уже использована")

    def attach_detail_regions(
        self,
        item_id: str,
        attempt_id: int,
        regions: list[dict[str, Any]],
    ) -> None:
        if not 1 <= len(regions) <= 4:
            raise ValueError("Нужно сохранить от одной до четырёх областей")
        now = utc_now()
        with self.db.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    """
                    INSERT INTO detail_regions(
                        id, item_id, attempt_id, region_id, left_px, top_px,
                        right_px, bottom_px, crop_path, reason,
                        target_object_index, created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            str(uuid.uuid4()),
                            item_id,
                            attempt_id,
                            region["region_id"],
                            region["left"],
                            region["top"],
                            region["right"],
                            region["bottom"],
                            region.get("crop_path"),
                            region["reason"],
                            region.get("target_object_index"),
                            now,
                        )
                        for region in regions
                    ],
                )
                placeholders = ",".join("?" for _ in regions)
                conn.execute(
                    f"""
                    DELETE FROM detail_regions
                    WHERE item_id=? AND attempt_id IS NULL
                      AND region_id IN ({placeholders})
                    """,
                    (item_id, *(region["region_id"] for region in regions)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

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
        run_id: str | None = None,
        cycle_no: int = 1,
        quality_attempt_no: int = 1,
        attempt_kind: str = "initial",
        trigger_reason: str = "initial",
        image_count: int = 1,
        raw_response_path: str | None = None,
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
                    timed_out, stdout, stderr, error_message, run_id, cycle_no,
                    quality_attempt_no, attempt_kind, trigger_reason, image_count,
                    raw_response_path
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    run_id,
                    cycle_no,
                    quality_attempt_no,
                    attempt_kind,
                    trigger_reason,
                    image_count,
                    raw_response_path,
                ),
            )
            return int(cursor.lastrowid)

    def next_cycle_number(self, item_id: str) -> int:
        self.get_item(item_id)
        row = self.db.fetch_one(
            """
            SELECT COALESCE(MAX(cycle_no), 0) + 1 AS cycle_no
            FROM attempts
            WHERE item_id=?
            """,
            (item_id,),
        )
        return int(row["cycle_no"]) if row else 1

    def update_attempt_analysis(
        self,
        attempt_id: int,
        *,
        annotation: dict[str, Any] | None,
        validation_errors: list[str],
        validation_warnings: list[str],
        review_reasons: list[str],
        preview_path: str | None,
        label_path: str | None,
        validation_path: str | None,
    ) -> None:
        changed = self.db.execute(
            """
            UPDATE attempts
            SET annotation_json=?, validation_errors=?,
                validation_warnings=?, review_reasons=?,
                preview_path=?, label_path=?, validation_path=?
            WHERE id=?
            """,
            (
                self.db.json_value(annotation) if annotation is not None else None,
                self.db.json_value(validation_errors),
                self.db.json_value(validation_warnings),
                self.db.json_value(review_reasons),
                preview_path,
                label_path,
                validation_path,
                attempt_id,
            ),
        )
        if changed != 1:
            raise QueueError("Попытка распознавания не найдена")

    def list_attempts(self, item_id: str) -> list[dict[str, Any]]:
        self.get_item(item_id)
        attempts = self.db.fetch_all(
            """
            SELECT attempts.*, revision.id AS revision_id
            FROM attempts
            LEFT JOIN annotation_revisions AS revision
              ON revision.attempt_id=attempts.id
            WHERE attempts.item_id=?
            ORDER BY attempts.cycle_no DESC,
                     attempts.quality_attempt_no DESC,
                     attempts.id DESC
            """,
            (item_id,),
        )
        for attempt in attempts:
            for key in (
                "annotation_json",
                "validation_errors",
                "validation_warnings",
                "review_reasons",
            ):
                fallback: dict[str, Any] | list[Any] | None
                fallback = None if key == "annotation_json" else []
                try:
                    attempt[key] = (
                        json.loads(attempt[key])
                        if attempt[key] is not None
                        else fallback
                    )
                except (TypeError, json.JSONDecodeError):
                    attempt[key] = fallback
            attempt["annotation"] = attempt.pop("annotation_json")
            attempt["timed_out"] = bool(attempt["timed_out"])
            attempt["selected"] = bool(attempt["selected"])
        return attempts

    def status_counts(self) -> dict[str, int]:
        rows = self.db.fetch_all("SELECT status, COUNT(*) AS count FROM items GROUP BY status")
        counts = {status.value: 0 for status in ItemStatus}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts
