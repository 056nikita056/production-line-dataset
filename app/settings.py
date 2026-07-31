from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MODULE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = MODULE_ROOT / "config" / "app.yaml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppConfig(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8098, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def local_host_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost"}:
            raise ValueError("MVP разрешает слушать только 127.0.0.1/localhost")
        return value


class PathsConfig(StrictModel):
    incoming: str = "data/incoming"
    processing: str = "data/processing"
    completed: str = "data/completed"
    review: str = "data/review"
    failed: str = "data/failed"
    exports: str = "data/exports"
    legacy: str = "data/legacy"
    logs: str = "logs"
    database: str = "queue.sqlite3"


class InputConfig(StrictModel):
    extensions: list[str] = [".jpg", ".jpeg", ".png"]
    max_file_mb: int = Field(default=20, gt=0, le=1024)
    max_pixels: int = Field(default=25_000_000, gt=0)

    @field_validator("extensions")
    @classmethod
    def supported_extensions_only(cls, values: list[str]) -> list[str]:
        normalized = [v.lower() if v.startswith(".") else f".{v.lower()}" for v in values]
        allowed = {".jpg", ".jpeg", ".png"}
        if not normalized or any(v not in allowed for v in normalized):
            raise ValueError("Допустимы только .jpg, .jpeg и .png")
        return sorted(set(normalized))


class WorkerConfig(StrictModel):
    concurrency: int = Field(default=1, ge=1, le=1)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class CodexConfig(StrictModel):
    executable: str = "codex"
    sandbox: str = "read-only"
    approval_policy: str = "never"
    ephemeral: bool = True

    @model_validator(mode="after")
    def least_privilege(self) -> "CodexConfig":
        if self.sandbox != "read-only" or self.approval_policy != "never":
            raise ValueError("Codex runner MVP обязан использовать read-only и never")
        return self


class AnnotationConfig(StrictModel):
    coordinate_scale: int = Field(default=1000, ge=100, le=10000)
    tray_min_visible_fraction: float = Field(default=0.20, ge=0, le=1)
    qr_require_fully_visible: bool = True
    qr_allow_occlusion: bool = False
    rectangle_tolerance: float = Field(default=0.18, gt=0, le=0.5)
    conflict_iou_threshold: float = Field(default=0.85, ge=0.5, le=1)
    line_use_camera_template: bool = False
    line_template: list[list[int]] = []
    line_expected_count: int | None = Field(default=None, ge=0, le=10)
    line_shift_tolerance: float = Field(default=80, ge=0, le=1000)
    review_blurred_objects: bool = True

    @field_validator("line_template")
    @classmethod
    def validate_line_template(cls, points: list[list[int]]) -> list[list[int]]:
        if points and not 4 <= len(points) <= 20:
            raise ValueError("Шаблон линии должен содержать 4–20 точек")
        for point in points:
            if len(point) != 2 or any(not isinstance(v, int) or v < 0 or v > 1000 for v in point):
                raise ValueError("Точки шаблона линии должны быть целыми в диапазоне 0..1000")
        return points


class ReviewConfig(StrictModel):
    required_for_all_mvp_results: bool = True

    @field_validator("required_for_all_mvp_results")
    @classmethod
    def manual_review_is_mandatory(cls, value: bool) -> bool:
        if not value:
            raise ValueError("В MVP ручная проверка обязательна")
        return value


class Settings(StrictModel):
    app: AppConfig = AppConfig()
    paths: PathsConfig = PathsConfig()
    input: InputConfig = InputConfig()
    worker: WorkerConfig = WorkerConfig()
    codex: CodexConfig = CodexConfig()
    annotation: AnnotationConfig = AnnotationConfig()
    review: ReviewConfig = ReviewConfig()
    root: Path = MODULE_ROOT
    config_path: Path = DEFAULT_CONFIG

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    def path(self, name: str) -> Path:
        raw = getattr(self.paths, name)
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return safe_resolve(self.root, candidate)

    @property
    def prompt_path(self) -> Path:
        return safe_resolve(self.root, self.root / "config" / "annotation_rules.md")

    @property
    def schema_path(self) -> Path:
        return safe_resolve(self.root, self.root / "config" / "annotation_output.schema.json")

    @property
    def detail_schema_path(self) -> Path:
        return safe_resolve(self.root, self.root / "config" / "detail_output.schema.json")

    @property
    def classes_path(self) -> Path:
        return safe_resolve(self.root, self.root / "config" / "classes.txt")

    def ensure_directories(self) -> None:
        for name in (
            "incoming",
            "processing",
            "completed",
            "review",
            "failed",
            "exports",
            "legacy",
            "logs",
        ):
            self.path(name).mkdir(parents=True, exist_ok=True)
        self.path("database").parent.mkdir(parents=True, exist_ok=True)


def safe_resolve(root: Path, candidate: Path, *, must_exist: bool = False) -> Path:
    root_resolved = root.resolve(strict=True)
    candidate_abs = candidate if candidate.is_absolute() else root_resolved / candidate
    lexical = Path(os.path.abspath(candidate_abs))
    try:
        lexical_parts = lexical.relative_to(root_resolved).parts
    except ValueError as exc:
        raise ValueError(f"Путь выходит за корневой каталог: {candidate}") from exc
    current = root_resolved
    for part in lexical_parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"Символьные ссылки запрещены: {current.name}")
    resolved = candidate_abs.resolve(strict=must_exist)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"Путь выходит за корневой каталог: {candidate}") from exc
    return resolved


def load_settings(config_path: str | Path | None = None, *, root: Path | None = None) -> Settings:
    module_root = (root or MODULE_ROOT).resolve()
    raw_path = Path(config_path) if config_path else module_root / "config" / "app.yaml"
    if not raw_path.is_absolute():
        raw_path = module_root / raw_path
    path = safe_resolve(module_root, raw_path, must_exist=True)
    with path.open("r", encoding="utf-8") as handle:
        data: Any = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Конфигурация должна быть YAML-объектом")
    settings = Settings.model_validate({**data, "root": module_root, "config_path": path})
    settings.ensure_directories()
    return settings
