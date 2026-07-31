from __future__ import annotations

import copy
import time

from fastapi.testclient import TestClient
from PIL import Image

from app.agent_schema import AgentAnnotation
from app.codex_runner import FakeCodexRunner
from app.main import create_app
from app.validator import validate_annotation


def _wait_idle(client: TestClient) -> None:
    deadline = time.monotonic() + 5
    while client.get("/api/health").json()["worker_busy"]:
        if time.monotonic() >= deadline:
            raise AssertionError("Worker не завершился")
        time.sleep(0.02)


def _review_item(client: TestClient, settings, image_factory):
    image_factory(settings.path("incoming") / "manual.jpg", (160, 90))
    assert client.post("/api/scan").json()["added"] == 1
    assert client.post("/api/runs").status_code == 202
    _wait_idle(client)
    return client.get("/api/items").json()[0]


def _five_point_annotation(valid_payload):
    annotation = copy.deepcopy(valid_payload)
    annotation["objects"][0]["polygon"] = [
        {"x": 100, "y": 200},
        {"x": 300, "y": 180},
        {"x": 450, "y": 320},
        {"x": 350, "y": 520},
        {"x": 120, "y": 480},
    ]
    annotation["needs_review"] = False
    annotation["review_reasons"] = []
    return annotation


def test_manual_validation_allows_4_to_20_points_but_automatic_does_not(
    settings,
    valid_payload,
):
    annotation = AgentAnnotation.model_validate(
        _five_point_annotation(valid_payload)
    )

    manual = validate_annotation(
        annotation,
        settings,
        expected_width=160,
        expected_height=90,
        manual=True,
    )
    automatic = validate_annotation(
        annotation,
        settings,
        expected_width=160,
        expected_height=90,
    )

    assert manual.valid
    assert "object_0:requires_four_points" in automatic.errors


def test_manual_validation_rejects_self_intersection(
    settings,
    valid_payload,
):
    changed = copy.deepcopy(valid_payload)
    changed["objects"][0]["polygon"] = [
        {"x": 100, "y": 100},
        {"x": 500, "y": 500},
        {"x": 100, "y": 500},
        {"x": 500, "y": 100},
    ]
    result = validate_annotation(
        AgentAnnotation.model_validate(changed),
        settings,
        expected_width=160,
        expected_height=90,
        manual=True,
    )
    assert "object_0:self_intersection" in result.errors


def test_manual_draft_persists_and_valid_save_selects_revision_without_codex(
    settings,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload)
    app = create_app(settings, runner=runner)
    with TestClient(app) as client:
        item = _review_item(client, settings, image_factory)
        original_selected = client.get(f"/api/items/{item['id']}").json()[
            "selected_revision_id"
        ]
        draft = client.post(
            f"/api/items/{item['id']}/revisions/manual",
            json={"start_empty": False},
        )
        assert draft.status_code == 201
        draft = draft.json()
        assert draft["is_draft"]
        annotation = _five_point_annotation(valid_payload)
        saved = client.put(
            f"/api/items/{item['id']}/revisions/{draft['id']}/draft",
            json=annotation,
        )
        assert saved.status_code == 200
        assert saved.json()["annotation"]["objects"][0]["polygon"] == annotation["objects"][0]["polygon"]
        assert runner.calls == 1

    restarted = create_app(settings, runner=runner)
    with TestClient(restarted) as client:
        revisions = client.get(f"/api/items/{item['id']}/revisions").json()
        persisted = next(revision for revision in revisions if revision["id"] == draft["id"])
        assert persisted["is_draft"]
        result = client.post(
            f"/api/items/{item['id']}/revisions/{draft['id']}/validate",
            json=annotation,
        )
        assert result.status_code == 200
        body = result.json()
        assert body["validation"]["valid"]
        assert not body["revision"]["is_draft"]
        assert body["item"]["selected_revision_id"] == draft["id"]
        assert body["item"]["selected_revision_id"] != original_selected
        assert body["revision"]["preview_url"]
        assert body["revision"]["has_label"]
        stored = restarted.state.services.queue.get_revision(
            item["id"],
            draft["id"],
        )
        label = (settings.root / stored["label_path"]).read_text(
            encoding="utf-8"
        ).strip().split()
        assert len(label) == 11
        with Image.open(settings.root / stored["preview_path"]) as preview:
            assert preview.size == (160, 90)
        assert runner.calls == 1


def test_invalid_manual_save_remains_draft_and_does_not_replace_selection(
    settings,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload)
    app = create_app(settings, runner=runner)
    with TestClient(app) as client:
        item = _review_item(client, settings, image_factory)
        selected = client.get(f"/api/items/{item['id']}").json()[
            "selected_revision_id"
        ]
        draft = client.post(
            f"/api/items/{item['id']}/revisions/manual",
            json={"start_empty": True},
        ).json()
        invalid = copy.deepcopy(valid_payload)
        invalid["objects"][0]["polygon"] = [
            {"x": 100, "y": 100},
            {"x": 500, "y": 500},
            {"x": 100, "y": 500},
            {"x": 500, "y": 100},
        ]

        result = client.post(
            f"/api/items/{item['id']}/revisions/{draft['id']}/validate",
            json=invalid,
        )

        assert result.status_code == 200
        body = result.json()
        assert not body["validation"]["valid"]
        assert "object_0:self_intersection" in body["validation"]["errors"]
        assert body["revision"]["is_draft"]
        assert body["item"]["selected_revision_id"] == selected
        assert runner.calls == 1


def test_failed_recognition_can_be_replaced_with_manual_annotation(
    settings,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload, exit_code=9)
    app = create_app(settings, runner=runner)
    image_factory(settings.path("incoming") / "failed-manual.jpg", (160, 90))
    with TestClient(app) as client:
        client.post("/api/scan")
        client.post("/api/runs")
        _wait_idle(client)
        item = client.get("/api/items").json()[0]
        assert item["status"] == "failed"
        draft = client.post(
            f"/api/items/{item['id']}/revisions/manual",
            json={"start_empty": True},
        )
        assert draft.status_code == 201

        result = client.post(
            f"/api/items/{item['id']}/revisions/{draft.json()['id']}/validate",
            json=valid_payload,
        )

        assert result.status_code == 200
        assert result.json()["validation"]["valid"]
        assert result.json()["item"]["status"] == "review"
        assert result.json()["item"]["selected_revision_id"] == draft.json()["id"]
        assert runner.calls == 1
