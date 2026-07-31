from __future__ import annotations

import json
import os
import stat
import zipfile
from pathlib import Path

from app.codex_runner import CodexRunner, FakeCodexRunner
from app.db import Database
from app.export_bundle import ExportService
from app.queue import QueueRepository
from app.scanner import Scanner
from app.worker import Worker


def test_codex_command_is_safe_and_structured(settings, image_factory, tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    settings.codex.executable = str(executable)
    image = image_factory(settings.path("incoming") / "a.jpg")
    raw = settings.path("processing") / "raw.json"
    command = CodexRunner(settings).build_command(image, raw)
    assert command[:4] == [str(executable), "--ask-for-approval", "never", "exec"]
    assert 'approval_policy="never"' in command
    assert "--image" in command
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert "--sandbox" in command
    assert "shell=True" not in command
    assert "КАЛИБРОВКА ФИКСИРОВАННОЙ КАМЕРЫ CAM7-REFT" in command[-1]
    assert "(171, 0) → (312, 0) → (312, 1000) → (171, 1000)" in command[-1]
    assert "Не создавай объекты класса `line`" in command[-1]


def test_runner_nonzero_exit(settings, image_factory, tmp_path):
    executable = tmp_path / "codex-fail"
    executable.write_text("#!/bin/sh\necho failed >&2\nexit 7\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    settings.codex.executable = str(executable)
    image = image_factory(settings.path("incoming") / "a.jpg")
    result = CodexRunner(settings).run(
        image,
        settings.path("processing") / "raw.json",
        settings.path("processing") / "stderr.log",
    )
    assert result.exit_code == 7
    assert "failed" in result.stderr


def test_runner_timeout(settings, image_factory, tmp_path):
    executable = tmp_path / "codex-slow"
    executable.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    settings.codex.executable = str(executable)
    settings.worker.timeout_seconds = 1
    image = image_factory(settings.path("incoming") / "a.jpg")
    result = CodexRunner(settings).run(
        image,
        settings.path("processing") / "raw.json",
        settings.path("processing") / "stderr.log",
    )
    assert result.timed_out
    assert result.exit_code == 124


def _processed_item(settings, db, queue, image_factory, payload=None):
    image_factory(settings.path("incoming") / "frame.jpg", (160, 90))
    assert Scanner(settings, db, queue).scan().added == 1
    run = queue.create_run()
    worker = Worker(settings, db, queue, FakeCodexRunner(payload))
    result = worker.run(run["id"])
    item = queue.list_items()[0]
    return result, item, worker


def test_full_fake_worker_cycle(settings, db, queue, image_factory, valid_payload):
    run, item, _ = _processed_item(settings, db, queue, image_factory, valid_payload)
    assert run["status"] == "completed_with_review"
    assert item["status"] == "review"
    assert item["result_path"]
    assert item["preview_path"]
    assert (settings.root / item["result_path"]).is_file()
    assert (settings.root / item["preview_path"]).is_file()
    attempts = db.fetch_all("SELECT * FROM attempts WHERE item_id=?", (item["id"],))
    assert len(attempts) == 1


def test_worker_rejects_malformed_agent_json(settings, db, queue, image_factory):
    bad_runner = FakeCodexRunner({"bad": "shape"})
    image_factory(settings.path("incoming") / "bad.jpg")
    Scanner(settings, db, queue).scan()
    run = queue.create_run()
    Worker(settings, db, queue, bad_runner).run(run["id"])
    item = queue.list_items()[0]
    assert item["status"] == "failed"
    assert "agent_output_invalid" in item["validation_errors"]


def test_technical_retry_does_not_stop_batch(settings, db, queue, image_factory):
    image_factory(settings.path("incoming") / "one.jpg")
    image_factory(settings.path("incoming") / "two.jpg", color=(10, 20, 30))
    Scanner(settings, db, queue).scan()
    runner = FakeCodexRunner(exit_code=9)
    run = queue.create_run()
    Worker(settings, db, queue, runner).run(run["id"])
    assert runner.calls == 4
    assert {item["status"] for item in queue.list_items()} == {"failed"}


def test_export_contains_only_approved(settings, db, queue, image_factory, valid_payload):
    _, item, _ = _processed_item(settings, db, queue, image_factory, valid_payload)
    queue.approve(item["id"])
    image_factory(settings.path("incoming") / "unapproved.jpg", color=(30, 40, 50))
    Scanner(settings, db, queue).scan()
    export = ExportService(settings, db, queue).create()
    zip_path = settings.root / export["zip_path"]
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        assert "classes.txt" in names
        assert "notes.json" in names
        assert "manifest.json" in names
        assert sum(name.startswith("images/") for name in names) == 1
        assert sum(name.startswith("labels/") for name in names) == 1
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["image_count"] == 1
        assert manifest["object_counts"]["tray_filled"] == 1
