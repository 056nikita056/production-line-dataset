from __future__ import annotations

import hashlib
import time

import pytest
from PIL import Image

from app.agent_schema import (
    AgentAnnotation,
    AnnotationObject,
    DetailAgentResponse,
    Point,
)
from app.codex_runner import FakeCodexRunner
from app.detail import (
    DetailRegion,
    automatic_regions,
    create_crops,
    merge_detail_response,
    reproject_point,
)
from app.queue import QueueRepository
from app.scanner import Scanner
from app.worker import Worker


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _object(class_id=0, class_name="tray_filled", polygon=None):
    return AnnotationObject(
        class_id=class_id,
        class_name=class_name,
        polygon=polygon or [
            Point(x=200, y=200), Point(x=400, y=200),
            Point(x=400, y=400), Point(x=200, y=400),
        ],
        occluded=False,
        visible_fraction=1.0,
    )


def _annotation(objects):
    return AgentAnnotation(
        image_width=200,
        image_height=100,
        objects=objects,
        needs_review=False,
        review_reasons=[],
    )


def test_crop_has_exact_rectangle_pixels_and_does_not_change_original(tmp_path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (16, 12))
    for y in range(12):
        for x in range(16):
            image.putpixel((x, y), (x * 10, y * 20, x + y))
    image.save(source)
    before = _hash(source)
    region = DetailRegion("known", 3, 2, 13, 11, "test")

    [(saved_region, crop_path)] = create_crops(
        source, [region], tmp_path / "crops"
    )

    assert saved_region == region
    assert _hash(source) == before
    with Image.open(source) as original, Image.open(crop_path) as crop:
        assert crop.size == (10, 9)
        assert crop.tobytes() == original.crop((3, 2, 13, 11)).tobytes()


def test_crop_limit_is_four(tmp_path, image_factory):
    source = image_factory(tmp_path / "source.jpg", (100, 100))
    regions = [
        DetailRegion(f"r{index}", index, index, index + 20, index + 20, "test")
        for index in range(5)
    ]
    with pytest.raises(ValueError, match="четырёх"):
        create_crops(source, regions, tmp_path / "crops")


def test_crop_corner_reprojects_to_original_corner():
    region = DetailRegion("r", 50, 25, 150, 75, "test")
    assert reproject_point(Point(x=0, y=0), region, 200, 100) == Point(x=250, y=250)
    assert reproject_point(Point(x=1000, y=1000), region, 200, 100) == Point(x=750, y=750)


def test_automatic_regions_clip_margin_to_image_edges():
    annotation = _annotation([
        _object(polygon=[
            Point(x=0, y=0), Point(x=100, y=0),
            Point(x=100, y=100), Point(x=0, y=100),
        ])
    ])
    [region] = automatic_regions(annotation, 200, 100)
    assert region.left == 0
    assert region.top == 0
    assert region.right > 20
    assert region.bottom > 10


def test_merge_replaces_same_class_duplicate():
    base = _annotation([_object()])
    region = DetailRegion("r", 20, 10, 100, 50, "test", 0)
    detailed = _object(polygon=[
        Point(x=0, y=0), Point(x=1000, y=0),
        Point(x=1000, y=1000), Point(x=0, y=1000),
    ])
    response = DetailAgentResponse(
        regions=[{"region_id": "r", "objects": [detailed]}],
        needs_review=False,
        review_reasons=[],
    )

    merged = merge_detail_response(base, response, [region])

    assert len(merged.objects) == 1
    assert merged.objects[0].polygon[0] == Point(x=100, y=100)
    assert merged.objects[0].polygon[2] == Point(x=500, y=500)


def test_merge_marks_class_conflict_for_human():
    base = _annotation([_object()])
    region = DetailRegion("r", 40, 20, 80, 40, "test")
    different = _object(class_id=3, class_name="tray_empty", polygon=[
        Point(x=0, y=0), Point(x=1000, y=0),
        Point(x=1000, y=1000), Point(x=0, y=1000),
    ])
    response = DetailAgentResponse(
        regions=[{"region_id": "r", "objects": [different]}],
        needs_review=False,
        review_reasons=[],
    )

    merged = merge_detail_response(base, response, [region])

    assert len(merged.objects) == 2
    assert merged.needs_review
    assert "detail_class_conflict" in merged.review_reasons


def test_2x_detail_is_one_second_call_with_original_and_crop(
    settings,
    db,
    queue: QueueRepository,
    image_factory,
    valid_payload,
):
    first = dict(valid_payload)
    first["needs_review"] = True
    first["review_reasons"] = ["uncertain_object_boundary"]
    detail_object = dict(valid_payload["objects"][0])
    detail_object["polygon"] = [
        {"x": 100, "y": 100}, {"x": 900, "y": 100},
        {"x": 900, "y": 900}, {"x": 100, "y": 900},
    ]
    detail = {
        "regions": [{"region_id": "auto-01", "objects": [detail_object]}],
        "needs_review": False,
        "review_reasons": [],
    }
    runner = FakeCodexRunner([first, detail])
    source = image_factory(settings.path("incoming") / "detail.jpg", (160, 90))
    before = _hash(source)
    assert Scanner(settings, db, queue).scan().added == 1
    run = queue.create_run("auto_retry")

    finished = Worker(settings, db, queue, runner).run(run["id"])
    item = queue.list_items()[0]
    attempts = queue.list_attempts(item["id"])

    assert runner.calls == 2
    assert finished["codex_call_count"] == 2
    assert len(runner.image_paths_per_call[1]) == 2
    assert attempts[0]["attempt_kind"] == "detail"
    assert attempts[0]["image_count"] == 2
    assert len(queue.list_detail_regions(item["id"])) == 1
    assert _hash(source) == before


def test_detail_class_conflict_keeps_original_and_flags_item(
    settings,
    db,
    queue: QueueRepository,
    image_factory,
    valid_payload,
):
    first = dict(valid_payload)
    first["needs_review"] = True
    first["review_reasons"] = ["blurred_object"]
    conflicting = {
        "class_id": 3,
        "class_name": "tray_empty",
        "polygon": [
            {"x": 150, "y": 150}, {"x": 850, "y": 150},
            {"x": 850, "y": 850}, {"x": 150, "y": 850},
        ],
        "occluded": False,
        "visible_fraction": 1.0,
    }
    detail = {
        "regions": [{"region_id": "auto-01", "objects": [conflicting]}],
        "needs_review": False,
        "review_reasons": [],
    }
    runner = FakeCodexRunner([first, detail])
    image_factory(settings.path("incoming") / "conflict.jpg", (160, 90))
    Scanner(settings, db, queue).scan()
    run = queue.create_run("auto_retry")

    Worker(settings, db, queue, runner).run(run["id"])
    item = queue.list_items()[0]
    attempts = queue.list_attempts(item["id"])

    assert attempts[1]["selected"]
    assert attempts[1]["selection_reason"] == "detail_class_conflict"
    assert "detail_class_conflict" in item["review_reasons"]


def test_manual_detail_region_api_and_retry(
    settings,
    image_factory,
    valid_payload,
):
    from app.main import create_app
    from fastapi.testclient import TestClient

    runner = FakeCodexRunner(valid_payload)
    app = create_app(settings, runner=runner)
    image_factory(settings.path("incoming") / "manual-detail.jpg", (160, 90))
    with TestClient(app) as client:
        client.post("/api/scan")
        client.post("/api/runs")
        deadline = time.monotonic() + 5
        while client.get("/api/health").json()["worker_busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        item = client.get("/api/items").json()[0]
        created = client.post(
            f"/api/items/{item['id']}/detail-regions",
            json={"left": 10, "top": 12, "right": 80, "bottom": 60},
        )
        assert created.status_code == 201
        regions = client.get(f"/api/items/{item['id']}/detail-regions").json()
        assert regions[0]["left_px"] == 10
        region_id = created.json()["id"]
        assert client.delete(
            f"/api/items/{item['id']}/detail-regions/{region_id}"
        ).status_code == 204
        selected = client.post(
            f"/api/items/{item['id']}/detail-regions",
            json={"left": 20, "top": 10, "right": 100, "bottom": 70},
        ).json()
        runner.payload = [
            valid_payload,
            {
                "regions": [
                    {"region_id": selected["region_id"], "objects": []}
                ],
                "needs_review": False,
                "review_reasons": [],
            },
        ]
        retried = client.post(
            f"/api/items/{item['id']}/retry-detail",
            json={"recognition_mode": "single"},
        )
        assert retried.status_code == 202
        deadline = time.monotonic() + 5
        while client.get("/api/health").json()["worker_busy"]:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        attempts = client.get(f"/api/items/{item['id']}/attempts").json()
        assert runner.calls == 2
        assert attempts[0]["attempt_kind"] == "detail"
        assert attempts[0]["image_count"] == 2
