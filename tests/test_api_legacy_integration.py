from __future__ import annotations

import json
import os
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.codex_runner import CodexRunner, FakeCodexRunner
from app.db import Database
from app.legacy_import import LegacyImporter
from app.main import create_app
from app.queue import QueueRepository


def wait_until_idle(client: TestClient, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get("/api/health").json()["worker_busy"]:
            return
        time.sleep(0.05)
    raise AssertionError("Worker не завершился вовремя")


def test_api_full_cycle_and_no_absolute_paths(settings, image_factory, valid_payload):
    image_factory(settings.path("incoming") / "api.jpg", (160, 90))
    app = create_app(settings, runner=FakeCodexRunner(valid_payload))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"
        scan = client.post("/api/scan")
        assert scan.status_code == 200
        assert scan.json()["added"] == 1
        run = client.post("/api/runs")
        assert run.status_code == 202
        wait_until_idle(client)
        items = client.get("/api/items").json()
        assert len(items) == 1
        assert items[0]["status"] == "review"
        serialized = json.dumps(items[0], ensure_ascii=False)
        assert str(settings.root) not in serialized
        item_id = items[0]["id"]
        detail = client.get(f"/api/items/{item_id}")
        assert detail.json()["annotation"]["objects"]
        assert client.get(f"/review/{item_id}").status_code == 200
        assert client.get(items[0]["preview_url"]).status_code == 200
        approved = client.post(f"/api/items/{item_id}/approve")
        assert approved.status_code == 200
        export = client.post("/api/exports")
        assert export.status_code == 201
        download = client.get(export.json()["download_url"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"


def test_api_reject_and_retry(settings, image_factory, valid_payload):
    image_factory(settings.path("incoming") / "retry.jpg", (160, 90))
    app = create_app(settings, runner=FakeCodexRunner(valid_payload))
    with TestClient(app) as client:
        client.post("/api/scan")
        client.post("/api/runs")
        wait_until_idle(client)
        item = client.get("/api/items").json()[0]
        assert client.post(f"/api/items/{item['id']}/reject").json()["status"] == "rejected"
        retry = client.post(f"/api/items/{item['id']}/retry")
        assert retry.status_code == 202
        wait_until_idle(client)
        assert client.get(f"/api/items/{item['id']}").json()["status"] == "review"


def test_dashboard_counts_processing_as_queued_and_lists_it_first(
    settings, image_factory, valid_payload
):
    image_factory(settings.path("incoming") / "active.jpg", (160, 90))
    app = create_app(settings, runner=FakeCodexRunner(valid_payload))
    with TestClient(app) as client:
        client.post("/api/scan")
        item = client.get("/api/items").json()[0]
        app.state.services.db.execute(
            "UPDATE items SET status='processing' WHERE id=?",
            (item["id"],),
        )

        dashboard = client.get("/api/dashboard").json()

        assert dashboard["queued_count"] == 1
        assert dashboard["counts"]["pending"] == 0
        assert dashboard["counts"]["processing"] == 1
        assert dashboard["items"][0]["id"] == item["id"]


def test_api_returns_next_frame_waiting_for_review(
    settings, image_factory, valid_payload
):
    image_factory(settings.path("incoming") / "first.jpg", (160, 90))
    image_factory(
        settings.path("incoming") / "second.jpg",
        (160, 90),
        color=(180, 190, 200),
    )
    app = create_app(settings, runner=FakeCodexRunner(valid_payload))
    with TestClient(app) as client:
        client.post("/api/scan")
        client.post("/api/runs")
        wait_until_idle(client)
        items = client.get("/api/items", params={"status": "review"}).json()
        assert len(items) == 2

        current, expected_next = items
        response = client.get(f"/api/items/{current['id']}/next-review")
        assert response.status_code == 200
        assert response.json()["item"]["id"] == expected_next["id"]

        assert client.post(f"/api/items/{current['id']}/approve").status_code == 200
        response = client.get(f"/api/items/{current['id']}/next-review")
        assert response.json()["item"]["id"] == expected_next["id"]

        assert client.post(f"/api/items/{expected_next['id']}/approve").status_code == 200
        response = client.get(f"/api/items/{expected_next['id']}/next-review")
        assert response.json()["item"] is None


def test_manual_correction_regenerates_outputs(settings, image_factory, valid_payload):
    image_factory(settings.path("incoming") / "edit.jpg", (160, 90))
    app = create_app(settings, runner=FakeCodexRunner(valid_payload))
    with TestClient(app) as client:
        client.post("/api/scan")
        client.post("/api/runs")
        wait_until_idle(client)
        item = client.get("/api/items").json()[0]
        detail = client.get(f"/api/items/{item['id']}").json()
        annotation = detail["annotation"]
        annotation["objects"][0]["class_id"] = 3
        annotation["objects"][0]["class_name"] = "tray_empty"
        response = client.post(
            f"/api/items/{item['id']}/annotation", json=annotation
        )
        assert response.status_code == 200
        assert response.json()["validation_errors"] == []
        assert response.json()["annotation"]["objects"][0]["class_name"] == "tray_empty"
        revisions = client.get(
            f"/api/items/{item['id']}/revisions"
        ).json()
        assert [revision["revision_no"] for revision in revisions] == [2, 1]
        assert revisions[0]["source"] == "manual"
        assert revisions[1]["source"] == "automatic"
        assert client.get(revisions[0]["preview_url"]).status_code == 200
        selected = client.post(
            f"/api/items/{item['id']}/revisions/{revisions[1]['id']}/select"
        )
        assert selected.status_code == 200
        assert (
            selected.json()["annotation"]["objects"][0]["class_name"]
            == "tray_filled"
        )


def test_legacy_import_zip_creates_review_and_report(
    settings, db, queue, image_factory, tmp_path
):
    legacy = tmp_path / "legacy-source"
    image_factory(legacy / "images" / "old.jpg", (100, 100))
    labels = legacy / "labels"
    labels.mkdir()
    (labels / "old.txt").write_text(
        "0 0.100000 0.100000 0.400000 0.100000 0.400000 0.400000 0.100000 0.400000\n"
        "2 0.600000 0.100000 0.800000 0.100000 0.800000 0.300000 0.600000 0.300000\n",
        encoding="utf-8",
    )
    (legacy / "classes.txt").write_text("chiken\nline\nqr code\n", encoding="utf-8")
    archive_path = tmp_path / "legacy.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for path in legacy.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(legacy))
    before = archive_path.read_bytes()
    report = LegacyImporter(settings, db, queue).import_path(archive_path)
    assert archive_path.read_bytes() == before
    assert report["imported"] == 1
    assert report["failed"] == 0
    assert (settings.root / report["report_path"]).is_file()
    item = queue.list_items()[0]
    assert item["status"] == "review"
    assert item["imported_legacy"] == 1
    raw = json.loads((settings.root / item["raw_response_path"]).read_text())
    assert raw["objects"][0]["class_name"] == "tray_filled"
    assert raw["objects"][1]["class_name"] == "qr_code"


def test_legacy_import_rejects_zip_traversal(settings, db, queue, tmp_path):
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../escape.jpg", b"bad")
    with pytest.raises(Exception, match="небезопасный"):
        LegacyImporter(settings, db, queue).import_path(archive_path)


@pytest.mark.real_codex
@pytest.mark.skipif(
    os.environ.get("RUN_REAL_CODEX_SMOKE") != "1",
    reason="Установите RUN_REAL_CODEX_SMOKE=1 для ручного реального smoke-теста",
)
def test_real_codex_smoke(settings, image_factory):
    image = image_factory(settings.path("incoming") / "real-smoke.jpg", (320, 180))
    runner = CodexRunner(settings)
    logged_in, detail = runner.login_status()
    assert logged_in, detail
    result = runner.run(
        image,
        settings.path("processing") / "real-smoke.json",
        settings.path("processing") / "real-smoke.stderr.log",
    )
    assert result.exit_code == 0, result.stderr
