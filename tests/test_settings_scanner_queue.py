from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.models import ItemStatus
from app.queue import InvalidTransition
from app.scanner import Scanner
from app.settings import safe_resolve


def test_configuration_loads_and_creates_directories(settings):
    assert settings.app.host == "127.0.0.1"
    assert settings.worker.concurrency == 1
    assert settings.path("incoming").is_dir()
    assert settings.path("database").parent == settings.root


def test_safe_path_rejects_escape_and_symlink(settings, tmp_path):
    with pytest.raises(ValueError, match="выходит"):
        safe_resolve(settings.root, settings.root / ".." / "outside")
    target = settings.root / "real"
    target.mkdir()
    link = settings.root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Символьные ссылки недоступны")
    with pytest.raises(ValueError, match="Символьные"):
        safe_resolve(settings.root, link / "file.txt")


def test_scanner_adds_images_and_deduplicates(settings, db, queue, image_factory):
    incoming = settings.path("incoming")
    image_factory(incoming / "frame.jpg")
    scanner = Scanner(settings, db, queue)
    first = scanner.scan()
    second = scanner.scan()
    assert first.added == 1
    assert second.added == 0
    assert second.duplicates == 1
    items = queue.list_items()
    assert len(items) == 1
    assert items[0]["status"] == "pending"
    copied = settings.root / items[0]["source_path"]
    assert copied.read_bytes() == (incoming / "frame.jpg").read_bytes()


def test_scanner_supports_png_and_rejects_fake_image(settings, db, queue, image_factory):
    incoming = settings.path("incoming")
    image_factory(incoming / "good.png")
    (incoming / "bad.jpg").write_text("not an image", encoding="utf-8")
    result = Scanner(settings, db, queue).scan()
    assert result.added == 1
    assert result.rejected == 1
    assert result.errors[0]["file"] == "bad.jpg"
    assert db.recent_errors(1)[0]["code"] == "invalid_input"


def test_scanner_rejects_symlink(settings, db, queue, image_factory, tmp_path):
    outside = image_factory(tmp_path.parent / f"outside-{uuid.uuid4().hex}.jpg")
    link = settings.path("incoming") / "link.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Символьные ссылки недоступны")
    result = Scanner(settings, db, queue).scan()
    assert result.rejected == 1
    assert result.added == 0


def _add(queue, item_id: str, sha: str, status: str = "pending"):
    assert queue.add_item(
        item_id=item_id,
        sha256=sha,
        original_name=f"{item_id}.jpg",
        source_path=f"data/processing/{item_id}/source/{item_id}.jpg",
        width=100,
        height=100,
        status=status,
    )


def test_status_transitions_and_invalid_transition(queue):
    _add(queue, "one", "a" * 64)
    run = queue.create_run()
    item = queue.claim_next(run["id"])
    assert item["status"] == "processing"
    queue.complete_processing(
        "one",
        result_path="result.txt",
        preview_path="preview.jpg",
        raw_response_path="raw.json",
        validation_path="validation.json",
        review_reasons=[],
        validation_errors=[],
    )
    assert queue.approve("one")["status"] == "approved"
    with pytest.raises(InvalidTransition):
        queue.reject("one")


def test_recover_processing_after_crash(queue):
    _add(queue, "crashed", "b" * 64)
    run = queue.create_run()
    assert queue.claim_next(run["id"])["status"] == "processing"
    assert queue.recover_processing() == 1
    recovered = queue.get_item("crashed")
    assert recovered["status"] == "pending"
    assert recovered["run_id"] is None


def test_sha_unique_constraint(queue):
    _add(queue, "first", "c" * 64)
    assert not queue.add_item(
        item_id="second",
        sha256="c" * 64,
        original_name="other.jpg",
        source_path="other.jpg",
        width=10,
        height=10,
    )

