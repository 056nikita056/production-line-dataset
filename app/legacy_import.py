from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from .agent_schema import AgentAnnotation
from .db import Database
from .preview import create_preview
from .queue import QueueRepository
from .scanner import sha256_file
from .settings import Settings
from .validator import validate_annotation
from .yolo_export import write_yolo


class LegacyImportError(RuntimeError):
    pass


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}


class LegacyImporter:
    def __init__(self, settings: Settings, db: Database, queue: QueueRepository):
        self.settings = settings
        self.db = db
        self.queue = queue

    def import_path(self, source: Path) -> dict[str, Any]:
        source = source.expanduser().resolve(strict=True)
        import_id = str(uuid.uuid4())
        destination = self.settings.path("legacy") / import_id
        destination.mkdir(parents=True, exist_ok=False)
        temporary_dir: Path | None = None
        try:
            if source.is_file() and source.suffix.lower() == ".zip":
                temporary_dir = Path(tempfile.mkdtemp(prefix="legacy-import-"))
                self._safe_extract(source, temporary_dir)
                root = temporary_dir
            elif source.is_dir() and not source.is_symlink():
                root = source
            else:
                raise LegacyImportError("Ожидался ZIP-архив или обычная папка")
            report = self._import_directory(root, destination, import_id)
            report["source"] = source.name
            report["source_sha256"] = sha256_file(source) if source.is_file() else None
            report_path = destination / "migration-report.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            report["report_path"] = report_path.relative_to(self.settings.root).as_posix()
            return report
        finally:
            if temporary_dir:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def _import_directory(
        self,
        root: Path,
        destination: Path,
        import_id: str,
    ) -> dict[str, Any]:
        images = self._index_files(root, IMAGE_EXTENSIONS)
        labels = self._index_files(root, {".txt"}, exclude_names={"classes.txt"})
        missing_labels = sorted(set(images) - set(labels))
        orphan_labels = sorted(set(labels) - set(images))
        report: dict[str, Any] = {
            "import_id": import_id,
            "status": "completed_with_review",
            "images_found": len(images),
            "labels_found": len(labels),
            "imported": 0,
            "duplicates": 0,
            "failed": 0,
            "missing_labels": missing_labels,
            "orphan_labels": orphan_labels,
            "items": [],
            "class_migration": {
                "0": "chiken -> tray_filled",
                "1": "line (сохранён только если был размечен)",
                "2": "qr code -> qr_code",
                "3": "tray_empty (добавлен в список классов)",
            },
            "warnings": [
                "Все мигрированные кадры требуют ручной проверки.",
                "Пустые лотки, ранее размеченные классом 0, нужно вручную заменить на класс 3.",
                "Аннотации line не создавались автоматически.",
            ],
        }
        if not images:
            raise LegacyImportError("В архиве не найдено JPEG/PNG")
        for stem in sorted(set(images) & set(labels)):
            image_path = images[stem]
            label_path = labels[stem]
            try:
                item_result = self._import_pair(
                    image_path, label_path, destination, import_id
                )
                report["items"].append(item_result)
                if item_result["status"] == "duplicate":
                    report["duplicates"] += 1
                else:
                    report["imported"] += 1
            except (OSError, ValueError, LegacyImportError) as exc:
                report["failed"] += 1
                report["items"].append(
                    {"stem": stem, "status": "failed", "error": str(exc)}
                )
                self.db.record_error("legacy_import", "invalid_legacy_pair", str(exc), stem)
        return report

    def _import_pair(
        self,
        image_path: Path,
        label_path: Path,
        destination: Path,
        import_id: str,
    ) -> dict[str, Any]:
        if image_path.is_symlink() or label_path.is_symlink():
            raise LegacyImportError("Символьные ссылки в legacy-наборе запрещены")
        with Image.open(image_path) as image:
            image.load()
            if image.format not in {"JPEG", "PNG"}:
                raise LegacyImportError("Неподдерживаемый формат изображения")
            width, height = image.size
        digest = sha256_file(image_path)
        if self.db.fetch_one("SELECT id FROM items WHERE sha256=?", (digest,)):
            return {"stem": image_path.stem, "status": "duplicate", "sha256": digest}
        annotation = self._parse_yolo(label_path, width, height)
        item_id = str(uuid.uuid4())
        item_root = destination / "items" / item_id
        source_dir = item_root / "source"
        output_dir = item_root / "output"
        agent_dir = item_root / "agent"
        source_dir.mkdir(parents=True, exist_ok=False)
        output_dir.mkdir(parents=True, exist_ok=False)
        agent_dir.mkdir(parents=True, exist_ok=False)
        copied_image = source_dir / image_path.name
        shutil.copy2(image_path, copied_image)
        raw_path = agent_dir / "raw_response.json"
        raw_path.write_text(annotation.model_dump_json(indent=2), encoding="utf-8")
        validation = validate_annotation(
            annotation,
            self.settings,
            expected_width=width,
            expected_height=height,
        )
        validation_path = output_dir / "validation.json"
        validation_path.write_text(
            json.dumps(validation.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        label_out = output_dir / f"{copied_image.stem}.txt"
        preview_path = output_dir / f"{copied_image.stem}.preview.jpg"
        if validation.valid:
            write_yolo(annotation, label_out, self.settings.annotation.coordinate_scale)
        create_preview(copied_image, annotation, preview_path)
        created = self.queue.add_item(
            item_id=item_id,
            sha256=digest,
            original_name=image_path.name,
            source_path=copied_image.relative_to(self.settings.root).as_posix(),
            width=width,
            height=height,
            imported_legacy=True,
            status="review",
        )
        if not created:
            return {"stem": image_path.stem, "status": "duplicate", "sha256": digest}
        self.queue.create_revision(
            item_id,
            annotation=annotation.model_dump(mode="json"),
            source="legacy_import",
            attempt_id=None,
            result_path=label_out.relative_to(self.settings.root).as_posix()
            if validation.valid
            else None,
            preview_path=preview_path.relative_to(self.settings.root).as_posix(),
            raw_response_path=raw_path.relative_to(self.settings.root).as_posix(),
            validation_path=validation_path.relative_to(self.settings.root).as_posix(),
            review_reasons=[],
            validation_errors=validation.errors,
            validation_warnings=validation.warnings,
            error_message=None if validation.valid else "Ошибки legacy-разметки",
            transition_to_review=False,
        )
        return {
            "stem": image_path.stem,
            "status": "review",
            "item_id": item_id,
            "sha256": digest,
            "objects": len(annotation.objects),
            "validation_errors": validation.errors,
        }

    @staticmethod
    def _parse_yolo(label_path: Path, width: int, height: int) -> AgentAnnotation:
        objects: list[dict[str, Any]] = []
        class_names = {0: "tray_filled", 1: "line", 2: "qr_code", 3: "tray_empty"}
        for line_no, raw_line in enumerate(
            label_path.read_text(encoding="utf-8-sig").splitlines(), start=1
        ):
            stripped = raw_line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            try:
                class_id = int(parts[0])
                coordinates = [float(value) for value in parts[1:]]
            except ValueError as exc:
                raise LegacyImportError(f"Строка {line_no}: нечисловое значение") from exc
            if class_id not in class_names:
                raise LegacyImportError(f"Строка {line_no}: неизвестный class_id {class_id}")
            if len(coordinates) < 8 or len(coordinates) % 2:
                raise LegacyImportError(f"Строка {line_no}: неверное число координат")
            if any(value < 0 or value > 1 for value in coordinates):
                raise LegacyImportError(f"Строка {line_no}: координата вне 0..1")
            point_count = len(coordinates) // 2
            if class_id in {0, 2, 3} and point_count != 4:
                raise LegacyImportError(f"Строка {line_no}: требуется 4 точки")
            if class_id == 1 and not 4 <= point_count <= 20:
                raise LegacyImportError(f"Строка {line_no}: line требует 4–20 точек")
            points = [
                {
                    "x": min(1000, max(0, round(coordinates[index] * 1000))),
                    "y": min(1000, max(0, round(coordinates[index + 1] * 1000))),
                }
                for index in range(0, len(coordinates), 2)
            ]
            objects.append(
                {
                    "class_id": class_id,
                    "class_name": class_names[class_id],
                    "polygon": points,
                    "occluded": False,
                    "visible_fraction": 1.0,
                }
            )
        return AgentAnnotation.model_validate(
            {
                "image_width": width,
                "image_height": height,
                "objects": objects,
                "needs_review": False,
                "review_reasons": [],
            }
        )

    @staticmethod
    def _index_files(
        root: Path,
        extensions: set[str],
        *,
        exclude_names: set[str] | None = None,
    ) -> dict[str, Path]:
        indexed: dict[str, Path] = {}
        excluded = {name.casefold() for name in (exclude_names or set())}
        for path in sorted(root.rglob("*")):
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in extensions
                and path.name.casefold() not in excluded
            ):
                if path.stem in indexed:
                    raise LegacyImportError(f"Повторяющийся basename: {path.stem}")
                indexed[path.stem] = path
        return indexed

    @staticmethod
    def _safe_extract(archive_path: Path, destination: Path) -> None:
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise LegacyImportError("ZIP содержит небезопасный путь")
                mode = member.external_attr >> 16
                if mode & 0o170000 == 0o120000:
                    raise LegacyImportError("ZIP содержит символьную ссылку")
                target = (destination / member_path).resolve()
                try:
                    target.relative_to(destination.resolve())
                except ValueError as exc:
                    raise LegacyImportError("ZIP пытается выйти из каталога импорта") from exc
            archive.extractall(destination)
