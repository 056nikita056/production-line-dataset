from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .agent_schema import AgentAnnotation, parse_agent_json, parse_detail_json
from .codex_runner import Runner
from .db import Database
from .detail import (
    DetailRegion,
    automatic_regions,
    create_crops,
    merge_detail_response,
)
from .models import RecognitionMode, ValidationResult
from .preview import create_preview
from .queue import QueueError, QueueRepository
from .recognition import (
    RecognitionCandidate,
    choose_candidate,
    retry_reason,
)
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
        run = (
            self.queue.get_run(item["run_id"])
            if item.get("run_id")
            else {
                "id": None,
                "recognition_mode": RecognitionMode.SINGLE,
                "max_auto_attempts": 1,
                "detail_requested": 0,
            }
        )
        max_calls = min(2, max(1, int(run["max_auto_attempts"])))
        cycle_no = self.queue.next_cycle_number(item_id)
        candidates: list[RecognitionCandidate] = []
        trigger = "manual_retry" if cycle_no > 1 else "initial"
        last_reason = "agent_failure"
        last_message = "Codex не вернул пригодный результат"
        last_raw_path: Path | None = None
        last_validation_path: Path | None = None
        manual_detail_base = (
            self._selected_annotation(item)
            if bool(run.get("detail_requested"))
            else None
        )

        for call_no in range(1, max_calls + 1):
            attempt_root = (
                self.settings.path("processing")
                / item_id
                / "cycles"
                / f"{cycle_no:04d}"
                / "attempts"
                / f"{call_no:02d}"
            )
            agent_dir = attempt_root / "agent"
            output_dir = attempt_root / "output"
            agent_dir.mkdir(parents=True, exist_ok=True)
            output_dir.mkdir(parents=True, exist_ok=True)
            raw_path = agent_dir / "raw_response.json"
            stderr_path = agent_dir / "stderr.log"
            detail_base: AgentAnnotation | None = None
            detail_regions: list[DetailRegion] = []
            detail_files: list[Path] = []
            detail_requested = bool(run.get("detail_requested")) and call_no == 1
            automatic_detail = (
                call_no > 1
                and trigger in {"blurred_object", "uncertain_object_boundary"}
                and bool(candidates)
            )
            if detail_requested or automatic_detail:
                detail_base = (
                    manual_detail_base
                    if detail_requested
                    else candidates[-1].annotation
                )
                if detail_base is None:
                    detail_base = AgentAnnotation(
                        image_width=item["width"],
                        image_height=item["height"],
                        objects=[],
                        needs_review=True,
                        review_reasons=[],
                    )
                pending = (
                    self.queue.list_detail_regions(item_id, pending_only=True)
                    if detail_requested
                    else []
                )
                if pending:
                    detail_regions = [
                        DetailRegion(
                            region_id=region["region_id"],
                            left=region["left_px"],
                            top=region["top_px"],
                            right=region["right_px"],
                            bottom=region["bottom_px"],
                            reason=region["reason"],
                            target_object_index=region["target_object_index"],
                        )
                        for region in pending[:4]
                    ]
                else:
                    detail_regions = automatic_regions(
                        detail_base,
                        item["width"],
                        item["height"],
                    )
                crops = create_crops(
                    source,
                    detail_regions,
                    attempt_root / "crops",
                )
                detail_files = [crop_path for _, crop_path in crops]
            detail_payload = [
                {
                    "region_id": region.region_id,
                    "left": region.left,
                    "top": region.top,
                    "right": region.right,
                    "bottom": region.bottom,
                    "reason": region.reason,
                    "target_object_index": region.target_object_index,
                }
                for region in detail_regions
            ]
            started_at = now_iso()
            result = self.runner.run(
                source,
                raw_path,
                stderr_path,
                retry_context=None if call_no == 1 else trigger,
                additional_image_paths=detail_files,
                detail_regions=detail_payload,
            )
            finished_at = now_iso()
            if detail_regions:
                attempt_kind = "detail"
            elif call_no == 1:
                attempt_kind = (
                    "manual_retry" if cycle_no > 1 else "initial"
                )
            elif trigger in {
                "agent_failure",
                "agent_output_invalid",
                "agent_timeout",
                "missing_agent_response",
            }:
                attempt_kind = "technical_retry"
            else:
                attempt_kind = "auto_retry"
            attempt_id = self.queue.add_attempt(
                item_id,
                call_no,
                run_id=run["id"],
                cycle_no=cycle_no,
                quality_attempt_no=call_no,
                attempt_kind=attempt_kind,
                trigger_reason=trigger,
                image_count=1 + len(detail_files),
                raw_response_path=(
                    self._relative(raw_path) if raw_path.exists() else None
                ),
                started_at=started_at,
                finished_at=finished_at,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                timed_out=result.timed_out,
                stdout=result.stdout,
                stderr=result.stderr,
                error_message=result.error,
            )
            if detail_regions:
                self.queue.attach_detail_regions(
                    item_id,
                    attempt_id,
                    [
                        {
                            **region,
                            "crop_path": self._relative(detail_files[index]),
                        }
                        for index, region in enumerate(detail_payload)
                    ],
                )
            last_raw_path = raw_path if raw_path.exists() else None

            if result.exit_code != 0 or not raw_path.is_file():
                if result.timed_out:
                    reason = "agent_timeout"
                elif result.exit_code == 0:
                    reason = "missing_agent_response"
                else:
                    reason = "agent_failure"
                message = result.error or reason
                self.queue.update_attempt_analysis(
                    attempt_id,
                    annotation=None,
                    validation_errors=[reason],
                    validation_warnings=[],
                    review_reasons=[reason],
                    preview_path=None,
                    label_path=None,
                    validation_path=None,
                )
                self.db.record_error("worker", reason, message, item_id)
                last_reason = reason
                last_message = message
                trigger = reason
                if call_no < max_calls and retry_reason(
                    technical_reason=reason
                ):
                    continue
                break

            try:
                raw_response = raw_path.read_text(encoding="utf-8")
                if detail_regions and detail_base is not None:
                    annotation = merge_detail_response(
                        detail_base,
                        parse_detail_json(raw_response),
                        detail_regions,
                    )
                else:
                    annotation = parse_agent_json(raw_response)
            except (ValidationError, ValueError, OSError) as exc:
                validation_path = output_dir / "validation.json"
                report = {
                    "valid": False,
                    "errors": ["agent_output_invalid"],
                    "warnings": [],
                    "detail": str(exc),
                }
                self._atomic_json(validation_path, report)
                self.queue.update_attempt_analysis(
                    attempt_id,
                    annotation=None,
                    validation_errors=["agent_output_invalid"],
                    validation_warnings=[],
                    review_reasons=["agent_output_invalid"],
                    preview_path=None,
                    label_path=None,
                    validation_path=self._relative(validation_path),
                )
                self.db.record_error(
                    "worker",
                    "agent_output_invalid",
                    str(exc),
                    item_id,
                )
                last_reason = "agent_output_invalid"
                last_message = "Ответ агента не прошёл структурную проверку"
                last_validation_path = validation_path
                trigger = last_reason
                if call_no < max_calls:
                    continue
                break

            updated = self.apply_annotation(
                item_id,
                annotation,
                raw_path=raw_path,
                attempt_id=attempt_id,
                transition_to_review=False,
            )
            revision_id = str(updated["selected_revision_id"])
            revision = self.queue.get_revision(item_id, revision_id)
            validation = ValidationResult(
                valid=not revision["validation_errors"],
                errors=revision["validation_errors"],
                warnings=revision["validation_warnings"],
            )
            self.queue.update_attempt_analysis(
                attempt_id,
                annotation=annotation.model_dump(mode="json"),
                validation_errors=validation.errors,
                validation_warnings=validation.warnings,
                review_reasons=list(annotation.review_reasons),
                preview_path=revision["preview_path"],
                label_path=revision["label_path"],
                validation_path=revision["validation_path"],
            )
            candidates.append(
                RecognitionCandidate(
                    revision_id=revision_id,
                    attempt_id=attempt_id,
                    annotation=annotation,
                    validation=validation,
                    detail=bool(detail_regions),
                )
            )
            reason = retry_reason(
                validation=validation,
                annotation=annotation,
            )
            if call_no >= max_calls or not reason:
                break
            trigger = reason

        if candidates:
            selected, selection_reason = choose_candidate(candidates)
            return self.queue.finalize_processing_revision(
                item_id,
                selected.revision_id,
                selection_reason=selection_reason,
            )

        return self.queue.complete_processing(
            item_id,
            result_path=None,
            preview_path=None,
            raw_response_path=(
                self._relative(last_raw_path) if last_raw_path else None
            ),
            validation_path=(
                self._relative(last_validation_path)
                if last_validation_path
                else None
            ),
            review_reasons=[last_reason],
            validation_errors=[last_reason],
            error_message=last_message,
            failed=True,
        )

    def apply_annotation(
        self,
        item_id: str,
        annotation: AgentAnnotation,
        *,
        raw_path: Path | None = None,
        manual: bool = False,
        attempt_id: int | None = None,
        transition_to_review: bool | None = None,
    ) -> dict[str, object]:
        item = self.queue.get_item(item_id)
        source = safe_resolve(
            self.settings.root,
            self.settings.root / item["source_path"],
            must_exist=True,
        )
        if raw_path is not None:
            revision_root = raw_path.parent.parent
        else:
            revision_no = self.queue.next_revision_number(item_id)
            revision_root = (
                self.settings.path("processing")
                / item_id
                / "revisions"
                / f"{revision_no:04d}"
            )
        output_dir = revision_root / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = Path(item["original_name"]).stem
        validation = validate_annotation(
            annotation,
            self.settings,
            expected_width=item["width"],
            expected_height=item["height"],
            manual=manual,
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
        raw_response = raw_path or revision_root / "agent" / "raw_response.json"
        if manual:
            raw_response.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_json(raw_response, annotation.model_dump(mode="json"))
        if transition_to_review is None:
            if item["status"] == "processing":
                transition_to_review = True
            elif manual and item["status"] == "review":
                transition_to_review = False
            else:
                raise RuntimeError(
                    "Исправлять аннотацию можно только в статусе review"
                )
        elif item["status"] != "processing":
            raise RuntimeError("Исправлять аннотацию можно только в статусе review")
        return self.queue.create_revision(
            item_id,
            annotation=annotation.model_dump(mode="json"),
            source="manual" if manual else "automatic",
            attempt_id=attempt_id,
            result_path=self._relative(result_path) if result_path else None,
            preview_path=self._relative(preview_path),
            raw_response_path=self._relative(raw_response),
            validation_path=self._relative(validation_path),
            review_reasons=reasons,
            validation_errors=validation.errors,
            validation_warnings=validation.warnings,
            error_message=(
                None
                if validation.valid
                else "Требуется исправить ошибки валидации"
            ),
            transition_to_review=transition_to_review,
        )

    def _selected_annotation(
        self,
        item: dict[str, object],
    ) -> AgentAnnotation | None:
        revision_id = item.get("selected_revision_id")
        if not revision_id:
            return None
        try:
            revision = self.queue.get_revision(
                str(item["id"]),
                str(revision_id),
            )
            return AgentAnnotation.model_validate(revision["annotation"])
        except (QueueError, ValueError, ValidationError):
            return None

    def save_manual_draft(
        self,
        item_id: str,
        revision_id: str,
        annotation: AgentAnnotation,
    ) -> dict[str, object]:
        return self.queue.update_manual_draft(
            item_id,
            revision_id,
            annotation.model_dump(mode="json"),
        )

    def validate_manual_draft(
        self,
        item_id: str,
        revision_id: str,
        annotation: AgentAnnotation,
    ) -> dict[str, object]:
        item = self.queue.get_item(item_id)
        revision = self.queue.get_revision(item_id, revision_id)
        if not revision.get("is_draft"):
            raise RuntimeError("Ревизия уже сохранена и больше не изменяется")
        source = safe_resolve(
            self.settings.root,
            self.settings.root / item["source_path"],
            must_exist=True,
        )
        revision_root = (
            self.settings.path("processing")
            / item_id
            / "revisions"
            / f"{revision['revision_no']:04d}"
        )
        agent_dir = revision_root / "agent"
        output_dir = revision_root / "output"
        agent_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        raw_path = agent_dir / "manual_annotation.json"
        validation_path = output_dir / "validation.json"
        preview_path = output_dir / f"{Path(item['original_name']).stem}.preview.jpg"
        label_path = output_dir / f"{Path(item['original_name']).stem}.txt"
        self._atomic_json(raw_path, annotation.model_dump(mode="json"))
        validation = validate_annotation(
            annotation,
            self.settings,
            expected_width=item["width"],
            expected_height=item["height"],
            manual=True,
        )
        self._atomic_json(validation_path, validation.to_dict())
        create_preview(source, annotation, preview_path)
        result_path: Path | None = None
        if validation.valid:
            write_yolo(
                annotation,
                label_path,
                coordinate_scale=self.settings.annotation.coordinate_scale,
            )
            result_path = label_path
        saved = self.queue.finalize_manual_draft(
            item_id,
            revision_id,
            annotation=annotation.model_dump(mode="json"),
            result_path=self._relative(result_path) if result_path else None,
            preview_path=self._relative(preview_path),
            raw_response_path=self._relative(raw_path),
            validation_path=self._relative(validation_path),
            review_reasons=list(annotation.review_reasons),
            validation_errors=validation.errors,
            validation_warnings=validation.warnings,
        )
        return {
            "revision": saved,
            "validation": validation.to_dict(),
            "item": self.queue.get_item(item_id),
        }

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
