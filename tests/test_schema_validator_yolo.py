from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent_schema import AgentAnnotation, parse_agent_json
from app.preview import create_preview
from app.validator import validate_annotation
from app.yolo_export import write_yolo, yolo_lines


def annotation(payload):
    return AgentAnnotation.model_validate(payload)


def test_parses_correct_json_and_rejects_extra_fields(valid_payload):
    parsed = parse_agent_json(json.dumps(valid_payload))
    assert parsed.image_width == 160
    invalid = dict(valid_payload)
    invalid["unexpected"] = True
    with pytest.raises(ValidationError):
        parse_agent_json(json.dumps(invalid))


def test_rejects_out_of_range_coordinate(valid_payload):
    valid_payload["objects"][0]["polygon"][0]["x"] = 1001
    with pytest.raises(ValidationError):
        annotation(valid_payload)


def test_validator_rejects_wrong_class_mapping(valid_payload, settings):
    valid_payload["objects"][0]["class_name"] = "tray_empty"
    result = validate_annotation(annotation(valid_payload), settings)
    assert not result.valid
    assert "object_0:class_id_name_mismatch" in result.errors


def test_validator_rejects_partial_and_occluded_qr(valid_payload, settings):
    valid_payload["objects"] = [
        {
            "class_id": 2,
            "class_name": "qr_code",
            "polygon": [
                {"x": 100, "y": 100}, {"x": 200, "y": 100},
                {"x": 200, "y": 200}, {"x": 100, "y": 200},
            ],
            "occluded": True,
            "visible_fraction": 0.8,
        }
    ]
    result = validate_annotation(annotation(valid_payload), settings)
    assert "object_0:qr_occluded" in result.errors
    assert "object_0:qr_not_fully_visible" in result.errors


def test_validator_rejects_tray_below_twenty_percent(valid_payload, settings):
    valid_payload["objects"][0]["visible_fraction"] = 0.19
    result = validate_annotation(annotation(valid_payload), settings)
    assert "object_0:tray_below_min_visibility" in result.errors


def test_validator_detects_duplicates_and_state_conflict(valid_payload, settings):
    filled = valid_payload["objects"][0]
    duplicate = json.loads(json.dumps(filled))
    empty = json.loads(json.dumps(filled))
    empty["class_id"] = 3
    empty["class_name"] = "tray_empty"
    valid_payload["objects"] = [filled, duplicate, empty]
    result = validate_annotation(annotation(valid_payload), settings)
    assert any("exact_duplicate" in error for error in result.errors)
    assert "tray_state_conflict" in result.errors
    assert "tray_state_conflict_requires_review" in result.errors


def test_validator_requires_rectangle(valid_payload, settings):
    valid_payload["objects"][0]["polygon"][2] = {"x": 200, "y": 300}
    result = validate_annotation(annotation(valid_payload), settings)
    assert "object_0:not_rectangle" in result.errors


def test_validator_accepts_rectangle_with_camera_perspective(valid_payload, settings):
    valid_payload["objects"][0]["polygon"] = [
        {"x": 100, "y": 210},
        {"x": 400, "y": 190},
        {"x": 370, "y": 510},
        {"x": 125, "y": 490},
    ]
    result = validate_annotation(annotation(valid_payload), settings)
    assert "object_0:not_rectangle" not in result.errors


def test_validator_compares_line_template(valid_payload, settings):
    settings.annotation.line_use_camera_template = True
    settings.annotation.line_template = [[0, 0], [100, 0], [100, 100], [0, 100]]
    settings.annotation.line_expected_count = 1
    valid_payload["objects"].append(
        {
            "class_id": 1,
            "class_name": "line",
            "polygon": [
                {"x": 50, "y": 550},
                {"x": 950, "y": 550},
                {"x": 950, "y": 900},
                {"x": 50, "y": 900},
            ],
            "occluded": False,
            "visible_fraction": 1.0,
        }
    )
    result = validate_annotation(annotation(valid_payload), settings)
    assert "object_1:camera_or_line_shift" in result.warnings
    assert "object_1:line_shift_requires_review" in result.errors


def test_validator_requires_calibrated_line_count(valid_payload, settings):
    settings.annotation.line_use_camera_template = True
    settings.annotation.line_expected_count = 0
    valid_payload["objects"].append(
        {
            "class_id": 1,
            "class_name": "line",
            "polygon": [
                {"x": 171, "y": 0},
                {"x": 312, "y": 0},
                {"x": 312, "y": 1000},
                {"x": 171, "y": 1000},
            ],
            "occluded": False,
            "visible_fraction": 1.0,
        }
    )
    result = validate_annotation(annotation(valid_payload), settings)
    assert "line_count_mismatch" in result.errors


def test_yolo_conversion_sorting_and_empty_file(valid_payload, tmp_path):
    parsed = annotation(valid_payload)
    lines = yolo_lines(parsed)
    assert len(lines) == 1
    assert lines[0].startswith("0 ")
    assert "0.100000" in lines[0]
    empty = annotation({
        "image_width": 10, "image_height": 10, "objects": [],
        "needs_review": False, "review_reasons": [],
    })
    path = write_yolo(empty, tmp_path / "empty.txt")
    assert path.read_text(encoding="utf-8") == ""


def test_yolo_skips_calibrated_line(valid_payload):
    valid_payload["objects"].append(
        {
            "class_id": 1,
            "class_name": "line",
            "polygon": [
                {"x": 171, "y": 0},
                {"x": 312, "y": 0},
                {"x": 312, "y": 1000},
                {"x": 171, "y": 1000},
            ],
            "occluded": False,
            "visible_fraction": 1.0,
        }
    )
    parsed = annotation(valid_payload)
    lines = yolo_lines(parsed)
    assert len(lines) == 1
    assert lines[0].startswith("0 ")


def test_yolo_remaps_active_classes_without_reserved_gap(valid_payload):
    valid_payload["objects"].extend(
        [
            {
                "class_id": 2,
                "class_name": "qr_code",
                "polygon": [
                    {"x": 500, "y": 100},
                    {"x": 600, "y": 100},
                    {"x": 600, "y": 200},
                    {"x": 500, "y": 200},
                ],
                "occluded": False,
                "visible_fraction": 1.0,
            },
            {
                "class_id": 3,
                "class_name": "tray_empty",
                "polygon": [
                    {"x": 100, "y": 500},
                    {"x": 300, "y": 500},
                    {"x": 300, "y": 700},
                    {"x": 100, "y": 700},
                ],
                "occluded": False,
                "visible_fraction": 1.0,
            },
        ]
    )
    class_ids = {int(line.split()[0]) for line in yolo_lines(annotation(valid_payload))}
    assert class_ids == {0, 1, 2}


def test_preview_generation(valid_payload, image_factory, tmp_path):
    source = image_factory(tmp_path / "source.jpg", (160, 90))
    destination = tmp_path / "preview.jpg"
    create_preview(source, annotation(valid_payload), destination)
    assert destination.is_file()
    assert destination.read_bytes() != source.read_bytes()
