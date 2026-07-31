from __future__ import annotations

import json
import platform as platform_module
import uuid
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .agent_schema import AgentAnnotation
from .db import Database, utc_now
from .models import CLASS_NAMES
from .platform_support import detect_platform
from .queue import QueueRepository
from .settings import Settings, safe_resolve
from .yolo_export import TRAINING_CLASS_IDS, yolo_lines


NOTES = {
    "categories": [
        {"id": class_id, "name": name}
        for name, class_id in TRAINING_CLASS_IDS.items()
    ],
    "info": {
        "year": 2026,
        "version": "2.0",
        "contributor": "Agent Labeling Automation",
    },
}


class ExportError(RuntimeError):
    pass


class ExportService:
    def __init__(self, settings: Settings, db: Database, queue: QueueRepository):
        self.settings = settings
        self.db = db
        self.queue = queue

    def create(self) -> dict[str, Any]:
        approved = self.queue.list_items(status="approved", limit=100_000)
        if not approved:
            raise ExportError("Нет подтверждённых кадров для экспорта")
        export_id = str(uuid.uuid4())
        exports_dir = self.settings.path("exports")
        zip_path = exports_dir / f"dataset-{export_id}.zip"
        temporary = zip_path.with_suffix(".zip.tmp")
        class_stats: Counter[str] = Counter()
        included: list[dict[str, Any]] = []
        used_names: set[str] = set()
        run_ids: set[str] = set()
        platform_info = detect_platform()
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for item in sorted(approved, key=lambda row: (row["original_name"].casefold(), row["id"])):
                revision_id = item.get("approved_revision_id")
                if not revision_id:
                    continue
                revision = self.queue.get_revision(item["id"], revision_id)
                if revision["validation_errors"] or not revision["label_path"]:
                    continue
                source = safe_resolve(
                    self.settings.root,
                    self.settings.root / item["source_path"],
                    must_exist=True,
                )
                label = safe_resolve(
                    self.settings.root,
                    self.settings.root / revision["label_path"],
                    must_exist=True,
                )
                annotation = AgentAnnotation.model_validate(revision["annotation"])
                for obj in annotation.objects:
                    if obj.class_name == "line":
                        continue
                    class_stats[obj.class_name] += 1
                image_name = self._unique_name(item["original_name"], item["id"], used_names)
                label_name = f"{Path(image_name).stem}.txt"
                archive.write(source, f"images/{image_name}")
                lines = yolo_lines(
                    annotation,
                    coordinate_scale=self.settings.annotation.coordinate_scale,
                )
                archive.writestr(
                    f"labels/{label_name}",
                    "\n".join(lines) + ("\n" if lines else ""),
                )
                included.append(
                    {
                        "item_id": item["id"],
                        "image": image_name,
                        "label": label_name,
                        "sha256": item["sha256"],
                        "run_id": item["run_id"],
                        "revision_id": revision_id,
                        "annotation_source": revision["source"],
                        **self._usage(item),
                    }
                )
                if item["run_id"]:
                    run_ids.add(item["run_id"])
            if not included:
                raise ExportError("Подтверждённые кадры не прошли проверку целостности")
            archive.writestr(
                "classes.txt", self.settings.classes_path.read_text(encoding="utf-8")
            )
            archive.writestr(
                "notes.json",
                json.dumps(NOTES, ensure_ascii=False, indent=2),
            )
            excluded = self.db.fetch_all(
                """
                SELECT id, original_name, status, error_message
                FROM items WHERE status != 'approved'
                ORDER BY created_at, id
                """
            )
            manifest = {
                "schema_version": "2.0",
                "application_version": __version__,
                "platform": {
                    "key": platform_info.key,
                    "label": platform_info.label,
                    "release": platform_module.release(),
                    "architecture": platform_info.architecture,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
                "image_count": len(included),
                "object_counts": {
                    name: class_stats.get(name, 0)
                    for name in CLASS_NAMES.values()
                    if name != "line"
                },
                "run_ids": sorted(run_ids),
                "items": included,
                "excluded": excluded,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        temporary.replace(zip_path)
        self.db.execute(
            "INSERT INTO exports(id,zip_path,item_count,class_stats,created_at) VALUES(?,?,?,?,?)",
            (
                export_id,
                zip_path.relative_to(self.settings.root).as_posix(),
                len(included),
                self.db.json_value(dict(class_stats)),
                utc_now(),
            ),
        )
        return self.get(export_id)

    def _usage(self, item: dict[str, Any]) -> dict[str, Any]:
        usage = self.db.fetch_one(
            """
            SELECT COUNT(*) AS codex_call_count,
                   COALESCE(SUM(duration_ms), 0) AS codex_duration_ms
            FROM attempts WHERE item_id=?
            """,
            (item["id"],),
        ) or {"codex_call_count": 0, "codex_duration_ms": 0}
        mode = None
        if item.get("run_id"):
            run = self.db.fetch_one(
                "SELECT recognition_mode FROM runs WHERE id=?",
                (item["run_id"],),
            )
            mode = run["recognition_mode"] if run else None
        return {
            "recognition_mode": mode,
            "codex_call_count": int(usage["codex_call_count"]),
            "codex_duration_ms": int(usage["codex_duration_ms"]),
        }

    def get(self, export_id: str) -> dict[str, Any]:
        record = self.db.fetch_one("SELECT * FROM exports WHERE id=?", (export_id,))
        if not record:
            raise ExportError("Экспорт не найден")
        record["class_stats"] = json.loads(record["class_stats"])
        return record

    def download_path(self, export_id: str) -> Path:
        record = self.get(export_id)
        return safe_resolve(
            self.settings.root,
            self.settings.root / record["zip_path"],
            must_exist=True,
        )

    @staticmethod
    def _unique_name(original: str, item_id: str, used: set[str]) -> str:
        safe = Path(original).name
        if safe not in used:
            used.add(safe)
            return safe
        candidate = f"{Path(safe).stem}__{item_id[:8]}{Path(safe).suffix.lower()}"
        used.add(candidate)
        return candidate
