from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import time
import tomllib
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import __version__
from app.codex_runner import FakeCodexRunner
from app.main import create_app
from app.settings import MODULE_ROOT


def _wait_until_idle(client: TestClient, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get("/api/health").json()["worker_busy"]:
            return
        time.sleep(0.02)
    raise AssertionError("Worker не завершился вовремя")


def test_release_e2e_2x_draft_restart_approve_and_export(
    settings,
    image_factory,
    valid_payload,
):
    source = image_factory(settings.path("incoming") / "release-e2e.jpg", (160, 90))
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    retryable = copy.deepcopy(valid_payload)
    retryable["objects"][0]["polygon"][2] = {"x": 200, "y": 300}
    runner = FakeCodexRunner([retryable, valid_payload])

    first_app = create_app(settings, runner=runner)
    with TestClient(first_app) as client:
        assert client.get("/api/health").json()["version"] == __version__
        assert [entry["id"] for entry in client.get("/api/settings/public").json()["classes"]] == [0, 1, 2]
        assert client.post("/api/scan").json()["added"] == 1
        run = client.post(
            "/api/runs",
            json={"recognition_mode": "auto_retry"},
        )
        assert run.status_code == 202
        _wait_until_idle(client)
        item = client.get("/api/items").json()[0]
        attempts = client.get(f"/api/items/{item['id']}/attempts").json()
        assert len(attempts) == 2
        assert runner.calls == 2
        assert attempts[0]["trigger_reason"] == "not_rectangle"

        draft = client.post(
            f"/api/items/{item['id']}/revisions/manual",
            json={"start_empty": True},
        ).json()
        manual = copy.deepcopy(valid_payload)
        manual["objects"][0].update(
            {
                "class_id": 3,
                "class_name": "tray_empty",
                "polygon": [
                    {"x": 100, "y": 200},
                    {"x": 350, "y": 180},
                    {"x": 470, "y": 350},
                    {"x": 340, "y": 520},
                    {"x": 120, "y": 480},
                ],
            }
        )
        saved = client.put(
            f"/api/items/{item['id']}/revisions/{draft['id']}/draft",
            json=manual,
        )
        assert saved.status_code == 200
        assert saved.json()["is_draft"]

    restarted_app = create_app(settings, runner=runner)
    with TestClient(restarted_app) as client:
        revisions = client.get(f"/api/items/{item['id']}/revisions").json()
        assert next(row for row in revisions if row["id"] == draft["id"])["is_draft"]
        validated = client.post(
            f"/api/items/{item['id']}/revisions/{draft['id']}/validate",
            json=manual,
        )
        assert validated.status_code == 200
        assert validated.json()["validation"]["valid"]
        approved = client.post(f"/api/items/{item['id']}/approve")
        assert approved.status_code == 200
        export = client.post("/api/exports")
        assert export.status_code == 201
        archive_path = restarted_app.state.services.exports.download_path(
            export.json()["id"]
        )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        labels = archive.read("labels/release-e2e.txt").decode("utf-8").strip()
        assert archive.read("classes.txt").decode("utf-8").splitlines() == [
            "tray_filled",
            "qr_code",
            "tray_empty",
        ]
    exported = manifest["items"][0]
    assert manifest["application_version"] == __version__
    assert manifest["platform"]["key"]
    assert exported["revision_id"] == draft["id"]
    assert exported["annotation_source"] == "manual"
    assert exported["recognition_mode"] == "auto_retry"
    assert exported["codex_call_count"] == 2
    assert labels.startswith("2 ")
    assert "line" not in json.dumps(manifest)


def test_release_version_is_consistent():
    project = tomllib.loads((MODULE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__ == "1.0.0"


def test_git_tracks_no_runtime_data():
    git_dir = MODULE_ROOT / ".git"
    if not git_dir.exists():
        return
    tracked = subprocess.run(
        ["git", "-C", str(MODULE_ROOT), "ls-files", "-z"],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8").split("\0")
    runtime_roots = ("data/", "logs/")
    forbidden = [
        path
        for path in tracked
        if (
            path == "queue.sqlite3"
            or path.startswith("queue.sqlite3-")
            or (
                path.startswith(runtime_roots)
                and not path.endswith("/.gitkeep")
            )
        )
    ]
    assert forbidden == []
