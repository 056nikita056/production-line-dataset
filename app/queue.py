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

    def update_review_artifacts(
        self,
        item_id: str,
        *,
        result_path: str | None,
        preview_path: str,
        raw_response_path: str,
        validation_path: str,
        review_reasons: list[str],
        validation_errors: list[str],
        error_message: str | None,
    ) -> dict[str, Any]:
        changed = self.db.execute(
            """
            UPDATE items SET result_path=?, preview_path=?, raw_response_path=?,
                validation_path=?, review_reasons=?, validation_errors=?,
                error_message=?, updated_at=?
            WHERE id=? AND status=?
            """,
            (
                result_path,
                preview_path,
                raw_response_path,
                validation_path,
                self.db.json_value(review_reasons),
                self.db.json_value(validation_errors),
                error_message,
                utc_now(),
                item_id,
                ItemStatus.REVIEW,
            ),
        )
        if changed != 1:
            raise InvalidTransition("Исправление доступно только для review")
        return self.get_item(item_id)

    def approve(self, item_id: str) -> dict[str, Any]:
        item = self.get_item(item_id)
        if item["validation_errors"]:
            raise QueueError("Нельзя принять результат с ошибками валидации")
        if not item["result_path"] or not item["preview_path"]:
            raise QueueError("Нельзя принять результат без label и preview")
        return self.transition(item_id, ItemStatus.APPROVED)

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
    ) -> dict[str, Any]:
        target = ItemStatus.FAILED if failed else ItemStatus.REVIEW
        item = self.get_item(item_id)
        if item["status"] != ItemStatus.PROCESSING:
            raise InvalidTransition(f"Ожидался processing, получен {item['status']}")
        self.db.execute(
            """
            UPDATE items SET status=?, result_path=?, preview_path=?, raw_response_path=?,
                validation_path=?, review_reasons=?, validation_errors=?, error_message=?,
                updated_at=? WHERE id=? AND status=?
            """,
            (
                target,
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
    ) -> None:
        self.db.execute(
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

    def status_counts(self) -> dict[str, int]:
        rows = self.db.fetch_all("SELECT status, COUNT(*) AS count FROM items GROUP BY status")
        counts = {status.value: 0 for status in ItemStatus}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts
