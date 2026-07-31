from __future__ import annotations

import hashlib
import shutil
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from .db import Database
from .queue import QueueRepository
from .settings import Settings, safe_resolve


class InputValidationError(ValueError):
    pass


@dataclass(slots=True)
class ScanResult:
    discovered: int = 0
    added: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "discovered": self.discovered,
            "added": self.added,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_image(path: Path, settings: Settings) -> tuple[int, int, str]:
    if path.is_symlink():
        raise InputValidationError("Символьные ссылки запрещены")
    safe_resolve(settings.path("incoming"), path, must_exist=True)
    if not path.is_file():
        raise InputValidationError("Это не обычный файл")
    if path.suffix.lower() not in settings.input.extensions:
        raise InputValidationError("Неподдерживаемое расширение")
    max_bytes = settings.input.max_file_mb * 1024 * 1024
    if path.stat().st_size > max_bytes:
        raise InputValidationError(
            f"Размер превышает лимит {settings.input.max_file_mb} МБ"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in {"JPEG", "PNG"}:
                    raise InputValidationError("Содержимое не является JPEG или PNG")
                if width * height > settings.input.max_pixels:
                    raise InputValidationError(
                        f"Количество пикселей превышает лимит {settings.input.max_pixels}"
                    )
                image.verify()
            with Image.open(path) as decoded:
                decoded.load()
    except InputValidationError:
        raise
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InputValidationError(f"Изображение повреждено или небезопасно: {exc}") from exc
    return width, height, image_format


class Scanner:
    def __init__(self, settings: Settings, db: Database, queue: QueueRepository):
        self.settings = settings
        self.db = db
        self.queue = queue

    def incoming_count(self) -> int:
        incoming = self.settings.path("incoming")
        return sum(
            1
            for path in incoming.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and path.suffix.lower() in self.settings.input.extensions
        )

    def scan(self) -> ScanResult:
        result = ScanResult()
        incoming = self.settings.path("incoming")
        candidates = sorted(
            (path for path in incoming.iterdir() if path.suffix.lower() in self.settings.input.extensions),
            key=lambda path: path.name.casefold(),
        )
        for path in candidates:
            result.discovered += 1
            try:
                width, height, _ = validate_image(path, self.settings)
                digest = sha256_file(path)
                if self.db.fetch_one("SELECT id FROM items WHERE sha256=?", (digest,)):
                    result.duplicates += 1
                    continue
                item_id = str(uuid.uuid4())
                item_root = self.settings.path("processing") / item_id
                source_dir = item_root / "source"
                source_dir.mkdir(parents=True, exist_ok=False)
                copied = source_dir / path.name
                shutil.copy2(path, copied)
                relative_source = copied.relative_to(self.settings.root).as_posix()
                created = self.queue.add_item(
                    item_id=item_id,
                    sha256=digest,
                    original_name=path.name,
                    source_path=relative_source,
                    width=width,
                    height=height,
                )
                if not created:
                    shutil.rmtree(item_root)
                    result.duplicates += 1
                    continue
                result.added += 1
            except (InputValidationError, OSError, ValueError) as exc:
                result.rejected += 1
                message = str(exc)
                result.errors.append({"file": path.name, "message": message})
                self.db.record_error("scan", "invalid_input", message, path.name)
        return result

