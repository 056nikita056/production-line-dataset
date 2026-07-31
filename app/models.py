from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any


class ItemStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    REVIEW = "review"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class RunStatus(StrEnum):
    CREATED = "created"
    SCANNING = "scanning"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_REVIEW = "completed_with_review"
    FAILED = "failed"


CLASS_NAMES: dict[int, str] = {
    0: "tray_filled",
    1: "line",
    2: "qr_code",
    3: "tray_empty",
}
CLASS_IDS = {name: class_id for class_id, name in CLASS_NAMES.items()}


@dataclass(slots=True)
class RunnerResult:
    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    errors: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

