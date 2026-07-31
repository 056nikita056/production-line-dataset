from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .agent_schema import AgentAnnotation, parse_agent_json
from .codex_runner import Runner
from .db import Database
from .preview import create_preview
from .queue import QueueRepository
from .settings import Settings, safe_resolve
from .validator import validate_annotation
from .yolo_export import write_yolo


logger = logging.getLogger(__name__)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Worker:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        queue: QueueRepository,
        runner: Runner,
    ):
        self.settings = settings
        self.db = db
        self.queue = queue
        self.runner = runner

    def run(self, run_id: str) -> dict[str, object]:
        self.queue.set_run_status(run_id, "processing")
        while True:
            item = self.queue.claim_next(run_id)
            if not item:
                break
            try:
                self.process_claimed(item["id"])
            except Exception as exc:
                logger.exception("Необработанная ошибка item=%s", item["id"])
                self.db.record_error("worker", "unexpected_failure", str(exc), item["id"])
                try:
                    self.queue.complete_processing(
                        item["id"],
                        result_path=None,
                        preview_path=None,
                        raw_response_path=None,
                        validation_path=None,
                        review_reasons=["agent_failure"],
                        validation_errors=["unexpected_worker_failure"],
                        error_message=str(exc),
                        failed=True,
                    )
                except Exception:
                    logger.exception("Не удалось зафиксировать ошибку item=%s", item["id"])
        return self.queue.finalize_run(run_id)

    def process_claimed(self, item_id: str) -> dict[str, object]:
        item = self.queue.get_item(item_id)
        if item["status"] != "processing":
            raise RuntimeError("Worker может обрабатывать только claimed item")
        source = safe_resolve(
            self.settings.root,
            self.settings.root / item["source_path"],
            must_exist=True,
        )
        item_root = self.settings.path("processing") / item_id
        agent_dir = item_root / "agent"
        output_dir = item_root / "output"
        agent_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = agent_dir / "raw_response.json"
        stderr_path = agent_dir / "stderr.log"

        final_result = None
        for attempt_no in range(1, self.settings.worker.technical_retries + 2):
            started_at = now_iso()
            result = self.runner.run(source, raw_path, stderr_path)
            finished_at = now_iso()
            self.queue.add_attempt(
                item_id,
                attempt_no,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                stdout=result.stdout,
                stderr=result.stderr,
                error_message=result.error,
            )
            final_result = result
            if result.exit_code == 0 and raw_path.is_file():
                break
        if final_result is None or final_result.exit_code != 0 or not raw_path.is_file():
            reason = "agent_timeout" if final_result and final_result.timed_out else "agent_failure"
            message = final_result.error if final_result else "Codex не вернул результат"
            self.db.record_error("worker", reason, message or reason, item_id)
            return self.queue.complete_processing(
                item_id,
                result_path=None,
                preview_path=None,
                raw_response_path=self._relative(raw_path) if raw_path.exists() else None,
                validation_path=None,
                review_reasons=[reason],
                validation_errors=[reason],
                error_message=message,
                failed=True,
            )

        try:
            annotation = parse_agent_json(raw_path.read_text(encoding="utf-8"))
        except (ValidationError, ValueError, OSError) as exc:
            validation_path = output_dir / "validation.json"
            report = {
                "valid": False,
                "errors": ["agent_output_invalid"],
                "warnings": [],
                "detail": str(exc),
            }
            self._atomic_json(validation_path, report)
            self.db.record_error("worker", "agent_output_invalid", str(exc), item_id)
            return self.queue.complete_processing(
                item_id,
                result_path=None,
                preview_path=None,
                raw_response_path=self._relative(raw_path),
                validation_path=self._relative(validation_path),
                review_reasons=["agent_output_invalid"],
                validation_errors=["agent_output_invalid"],
                error_message="Ответ агента не прошёл структурную проверку",
                failed=True,
            )
        return self.apply_annotation(item_id, annotation, raw_path=raw_path)

    def apply_annotation(
        self,
        item_id: str,
        annotation: AgentAnnotation,
        *,
        raw_path: Path | None = None,
        manual: bool = False,
    ) -> dict[str, object]:
        item = self.queue.get_item(item_id)
        source = safe_resolve(
            self.settings.root,
            self.settings.root / item["source_path"],
            must_exist=True,
        )
        output_dir = self.settings.path("processing") / item_id / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(item["original_name"]).stem
        validation = validate_annotation(
            annotation,
            self.settings,
            expected_width=item["width"],
            expected_height=item["height"],
        )
        validation_path = output_dir / "validation.json"
        self._atomic_json(validation_path, validation.to_dict())
        preview_path = output_dir / f"{stem}.preview.jpg"
        create_preview(source, annotation, preview_path)
        result_path: Path | None = None
        if validation.valid:
            result_path = output_dir / f"{stem}.txt"
            write_yolo(
                annotation,
                result_path,
                coordinate_scale=self.settings.annotation.coordinate_scale,
            )
        reasons = list(annotation.review_reasons)
        if manual:
            reasons = [reason for reason in reasons if reason != "agent_output_invalid"]
        raw_response = raw_path or self.settings.path("processing") / item_id / "agent" / "raw_response.json"
        if manual:
            raw_response.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_json(raw_response, annotation.model_dump(mode="json"))
        if item["status"] == "processing":
            return self.queue.complete_processing(
                item_id,
                result_path=self._relative(result_path) if result_path else None,
                preview_path=self._relative(preview_path),
                raw_response_path=self._relative(raw_response),
                validation_path=self._relative(validation_path),
                review_reasons=reasons,
                validation_errors=validation.errors,
                error_message=None if validation.valid else "Требуется исправить ошибки валидации",
                failed=False,
            )
        if manual and item["status"] == "review":
            return self.queue.update_review_artifacts(
                item_id,
                result_path=self._relative(result_path) if result_path else None,
                preview_path=self._relative(preview_path),
                raw_response_path=self._relative(raw_response),
                validation_path=self._relative(validation_path),
                review_reasons=reasons,
                validation_errors=validation.errors,
                error_message=None if validation.valid else "Требуется исправить ошибки валидации",
            )
        raise RuntimeError("Исправлять аннотацию можно только в статусе review")

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.settings.root).as_posix()

    @staticmethod
    def _atomic_json(path: Path, value: object) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

