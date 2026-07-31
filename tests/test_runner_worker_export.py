from __future__ import annotations

import copy
import json
import sys
import zipfile

import pytest

from app.agent_schema import AgentAnnotation
from app.codex_runner import CodexRunner, FakeCodexRunner
from app.db import Database
from app.export_bundle import ExportService
from app.process_control import ProcessExecution
from app.queue import QueueRepository
from app.scanner import Scanner
from app.worker import Worker


class StubProcessController:
    platform_name = "test"

    def __init__(self, execution: ProcessExecution):
        self.execution = execution
        self.commands: list[list[str]] = []

    def execute(self, command, *, cwd, timeout_seconds):
        self.commands.append(list(command))
        return self.execution


def test_codex_command_is_safe_and_structured(settings, image_factory):
    settings.codex.executable = sys.executable
    image = image_factory(settings.path("incoming") / "a.jpg")
    raw = settings.path("processing") / "raw.json"
    command = CodexRunner(settings).build_command(image, raw)
    assert command[:4] == [sys.executable, "--ask-for-approval", "never", "exec"]
    assert 'approval_policy="never"' in command
    assert "--image" in command
    assert "--output-schema" in command
    assert "--output-last-message" in command
    assert "--sandbox" in command
    assert "shell=True" not in command
    assert "КАЛИБРОВКА ФИКСИРОВАННОЙ КАМЕРЫ" not in command[-1]
    assert "Не создавай объект `line`" in command[-1]
    assert "кадры могут поступать" in command[-1]


def test_runner_detail_command_passes_original_and_at_most_four_crops(
    settings,
    image_factory,
):
    settings.codex.executable = sys.executable
    original = image_factory(settings.path("incoming") / "original.jpg")
    crops = [
        image_factory(settings.path("processing") / f"crop-{index}.png", (40, 40))
        for index in range(4)
    ]
    regions = [
        {
            "region_id": f"r{index}",
            "left": index,
            "top": index,
            "right": index + 40,
            "bottom": index + 40,
        }
        for index in range(4)
    ]

    command = CodexRunner(settings).build_command(
        original,
        settings.path("processing") / "detail.json",
        additional_image_paths=crops,
        detail_regions=regions,
    )

    assert command.count("--image") == 5
    schema_index = command.index("--output-schema") + 1
    assert command[schema_index] == str(settings.detail_schema_path)
    assert "region_id=r0" in command[-1]

    extra = image_factory(
        settings.path("processing") / "crop-extra.png",
        (40, 40),
    )
    with pytest.raises(ValueError, match="четырёх"):
        CodexRunner(settings).build_command(
            original,
            settings.path("processing") / "too-many.json",
            additional_image_paths=[*crops, extra],
            detail_regions=[
                *regions,
                {**regions[0], "region_id": "r4"},
            ],
        )


def test_runner_nonzero_exit(settings, image_factory):
    settings.codex.executable = sys.executable
    controller = StubProcessController(
        ProcessExecution(
            returncode=7,
            stdout="",
            stderr="failed",
            timed_out=False,
        )
    )
    image = image_factory(settings.path("incoming") / "a.jpg")
    result = CodexRunner(settings, process_controller=controller).run(
        image,
        settings.path("processing") / "raw.json",
        settings.path("processing") / "stderr.log",
    )
    assert result.exit_code == 7
    assert "failed" in result.stderr


def test_runner_timeout(settings, image_factory):
    settings.codex.executable = sys.executable
    controller = StubProcessController(
        ProcessExecution(
            returncode=-1,
            stdout="",
            stderr="",
            timed_out=True,
        )
    )
    settings.worker.timeout_seconds = 1
    image = image_factory(settings.path("incoming") / "a.jpg")
    result = CodexRunner(settings, process_controller=controller).run(
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
    revisions = queue.list_revisions(item["id"])
    assert len(revisions) == 1
    assert revisions[0]["id"] == item["selected_revision_id"]
    assert revisions[0]["source"] == "automatic"


def test_retry_creates_new_immutable_revision(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    _, item, worker = _processed_item(
        settings,
        db,
        queue,
        image_factory,
        valid_payload,
    )
    first = queue.list_revisions(item["id"])[0]
    first_raw = (
        settings.root / first["raw_response_path"]
    ).read_text(encoding="utf-8")

    queue.retry(item["id"])
    worker.process_claimed(item["id"])

    revisions = queue.list_revisions(item["id"])
    assert [revision["revision_no"] for revision in revisions] == [2, 1]
    assert revisions[0]["id"] != first["id"]
    assert revisions[0]["raw_response_path"] != first["raw_response_path"]
    assert (
        settings.root / first["raw_response_path"]
    ).read_text(encoding="utf-8") == first_raw
    assert queue.get_item(item["id"])["selected_revision_id"] == revisions[0]["id"]


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
    finished = Worker(settings, db, queue, runner).run(run["id"])
    assert runner.calls == 2
    assert finished["codex_call_count"] == 2
    assert {item["status"] for item in queue.list_items()} == {"failed"}


def test_export_contains_only_approved(settings, db, queue, image_factory, valid_payload):
    _, item, _ = _processed_item(settings, db, queue, image_factory, valid_payload)
    approved_item = queue.approve(item["id"])
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
        assert manifest["application_version"] == "1.0.0"
        assert manifest["platform"]["key"]
        assert manifest["platform"]["release"]
        assert manifest["object_counts"]["tray_filled"] == 1
        assert manifest["items"][0]["recognition_mode"] == "single"
        assert manifest["items"][0]["codex_call_count"] == 1
        assert manifest["items"][0]["codex_duration_ms"] >= 0
        assert (
            manifest["items"][0]["revision_id"]
            == approved_item["approved_revision_id"]
        )


def test_export_reads_locked_approved_revision(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    _, item, worker = _processed_item(
        settings,
        db,
        queue,
        image_factory,
        valid_payload,
    )
    first_revision = queue.list_revisions(item["id"])[0]
    manual_payload = copy.deepcopy(valid_payload)
    manual_payload["objects"][0]["class_id"] = 3
    manual_payload["objects"][0]["class_name"] = "tray_empty"
    worker.apply_annotation(
        item["id"],
        AgentAnnotation.model_validate(manual_payload),
        manual=True,
    )
    second_revision = queue.list_revisions(item["id"])[0]
    queue.select_revision(item["id"], first_revision["id"])
    approved = queue.approve(item["id"])
    assert approved["approved_revision_id"] == first_revision["id"]

    db.execute(
        """
        UPDATE items
        SET selected_revision_id=?, result_path=?, raw_response_path=?
        WHERE id=?
        """,
        (
            second_revision["id"],
            second_revision["label_path"],
            second_revision["raw_response_path"],
            item["id"],
        ),
    )
    export = ExportService(settings, db, queue).create()
    with zipfile.ZipFile(settings.root / export["zip_path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["items"][0]["revision_id"] == first_revision["id"]
    assert manifest["object_counts"]["tray_filled"] == 1
    assert manifest["object_counts"]["tray_empty"] == 0
