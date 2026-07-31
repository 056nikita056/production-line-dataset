from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.db import Database
from app.queue import QueueRepository
from app.settings import MODULE_ROOT, Settings, load_settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in (
        "app.yaml",
        "classes.txt",
        "annotation_rules.md",
        "annotation_output.schema.json",
    ):
        shutil.copy2(MODULE_ROOT / "config" / name, config_dir / name)
    return load_settings(root=tmp_path)


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.path("database"))
    database.initialize()
    return database


@pytest.fixture
def queue(db: Database) -> QueueRepository:
    return QueueRepository(db)


def make_image(path: Path, size: tuple[int, int] = (160, 90), color=(218, 210, 190)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def image_factory():
    return make_image


@pytest.fixture
def valid_payload() -> dict[str, object]:
    return {
        "image_width": 160,
        "image_height": 90,
        "objects": [
            {
                "class_id": 0,
                "class_name": "tray_filled",
                "polygon": [
                    {"x": 100, "y": 200},
                    {"x": 400, "y": 200},
                    {"x": 400, "y": 500},
                    {"x": 100, "y": 500},
                ],
                "occluded": False,
                "visible_fraction": 1.0,
            },
        ],
        "needs_review": False,
        "review_reasons": [],
    }
