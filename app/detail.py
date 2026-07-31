from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image

from .agent_schema import (
    AgentAnnotation,
    AnnotationObject,
    DetailAgentResponse,
    Point,
)


MAX_DETAIL_REGIONS = 4
DEFAULT_MARGIN = 0.20


@dataclass(frozen=True, slots=True)
class DetailRegion:
    region_id: str
    left: int
    top: int
    right: int
    bottom: int
    reason: str
    target_object_index: int | None = None

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def validate(self, image_width: int, image_height: int) -> None:
        if not (0 <= self.left < self.right <= image_width):
            raise ValueError("Некорректные горизонтальные границы фрагмента")
        if not (0 <= self.top < self.bottom <= image_height):
            raise ValueError("Некорректные вертикальные границы фрагмента")
        if self.width < 8 or self.height < 8:
            raise ValueError("Фрагмент должен быть не меньше 8 × 8 пикселей")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_region(
    obj: AnnotationObject,
    index: int,
    image_width: int,
    image_height: int,
    margin: float,
) -> DetailRegion:
    xs = [point.x / 1000 * image_width for point in obj.polygon]
    ys = [point.y / 1000 * image_height for point in obj.polygon]
    left, right = min(xs), max(xs)
    top, bottom = min(ys), max(ys)
    pad_x = max(4.0, (right - left) * margin)
    pad_y = max(4.0, (bottom - top) * margin)
    return DetailRegion(
        region_id=f"auto-{index + 1:02d}",
        left=max(0, math.floor(left - pad_x)),
        top=max(0, math.floor(top - pad_y)),
        right=min(image_width, math.ceil(right + pad_x)),
        bottom=min(image_height, math.ceil(bottom + pad_y)),
        reason="uncertain_object_boundary",
        target_object_index=index,
    )


def _fallback_tiles(image_width: int, image_height: int) -> list[DetailRegion]:
    overlap_x = max(1, round(image_width * 0.05))
    overlap_y = max(1, round(image_height * 0.05))
    middle_x = image_width // 2
    middle_y = image_height // 2
    rectangles = (
        (0, 0, min(image_width, middle_x + overlap_x), min(image_height, middle_y + overlap_y)),
        (max(0, middle_x - overlap_x), 0, image_width, min(image_height, middle_y + overlap_y)),
        (0, max(0, middle_y - overlap_y), min(image_width, middle_x + overlap_x), image_height),
        (max(0, middle_x - overlap_x), max(0, middle_y - overlap_y), image_width, image_height),
    )
    return [
        DetailRegion(
            region_id=f"tile-{index + 1:02d}",
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            reason="detail_fallback_tile",
        )
        for index, (left, top, right, bottom) in enumerate(rectangles)
    ]


def automatic_regions(
    annotation: AgentAnnotation,
    image_width: int,
    image_height: int,
    *,
    margin: float = DEFAULT_MARGIN,
) -> list[DetailRegion]:
    if not 0 <= margin <= 1:
        raise ValueError("Запас фрагмента должен быть от 0 до 1")
    candidates = list(
        enumerate(
            obj for obj in annotation.objects if obj.class_name != "line"
        )
    )
    candidates.sort(key=lambda pair: (pair[1].visible_fraction, pair[0]))
    if not candidates:
        return _fallback_tiles(image_width, image_height)
    regions = [
        _object_region(obj, index, image_width, image_height, margin)
        for index, obj in candidates[:MAX_DETAIL_REGIONS]
    ]
    for region in regions:
        region.validate(image_width, image_height)
    return regions


def create_crops(
    source_path: Path,
    regions: Iterable[DetailRegion],
    destination: Path,
) -> list[tuple[DetailRegion, Path]]:
    selected = list(regions)
    if not selected or len(selected) > MAX_DETAIL_REGIONS:
        raise ValueError("Нужно передать от одного до четырёх фрагментов")
    before = file_sha256(source_path)
    destination.mkdir(parents=True, exist_ok=True)
    created: list[tuple[DetailRegion, Path]] = []
    with Image.open(source_path) as image:
        width, height = image.size
        for region in selected:
            region.validate(width, height)
            crop_path = destination / f"{region.region_id}.png"
            image.crop(
                (region.left, region.top, region.right, region.bottom)
            ).save(crop_path, format="PNG")
            created.append((region, crop_path))
    if file_sha256(source_path) != before:
        raise RuntimeError("Исходное изображение неожиданно изменилось")
    return created


def reproject_point(
    point: Point,
    region: DetailRegion,
    image_width: int,
    image_height: int,
) -> Point:
    original_x = region.left + point.x / 1000 * region.width
    original_y = region.top + point.y / 1000 * region.height
    return Point(
        x=max(0, min(1000, round(original_x / image_width * 1000))),
        y=max(0, min(1000, round(original_y / image_height * 1000))),
    )


def reproject_object(
    obj: AnnotationObject,
    region: DetailRegion,
    image_width: int,
    image_height: int,
) -> AnnotationObject:
    return obj.model_copy(
        update={
            "polygon": [
                reproject_point(point, region, image_width, image_height)
                for point in obj.polygon
            ]
        }
    )


def _bbox(obj: AnnotationObject) -> tuple[int, int, int, int]:
    xs = [point.x for point in obj.polygon]
    ys = [point.y for point in obj.polygon]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_iou(first: AnnotationObject, second: AnnotationObject) -> float:
    a_left, a_top, a_right, a_bottom = _bbox(first)
    b_left, b_top, b_right, b_bottom = _bbox(second)
    intersection_width = max(0, min(a_right, b_right) - max(a_left, b_left))
    intersection_height = max(0, min(a_bottom, b_bottom) - max(a_top, b_top))
    intersection = intersection_width * intersection_height
    first_area = max(0, a_right - a_left) * max(0, a_bottom - a_top)
    second_area = max(0, b_right - b_left) * max(0, b_bottom - b_top)
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def merge_detail_response(
    base: AgentAnnotation,
    response: DetailAgentResponse,
    regions: list[DetailRegion],
) -> AgentAnnotation:
    by_id = {region.region_id: region for region in regions}
    if len(by_id) != len(regions):
        raise ValueError("Идентификаторы фрагментов должны быть уникальны")
    returned_ids = [entry.region_id for entry in response.regions]
    if len(set(returned_ids)) != len(returned_ids):
        raise ValueError("Ответ содержит повторяющийся region_id")
    returned = set(returned_ids)
    expected = set(by_id)
    if returned != expected:
        unknown = sorted(returned - expected)
        missing = sorted(expected - returned)
        raise ValueError(
            f"Набор region_id не совпадает: неизвестные={unknown}, пропущенные={missing}"
        )

    objects = [obj for obj in base.objects if obj.class_name != "line"]
    conflicts = False
    received_objects = False
    for region_result in response.regions:
        region = by_id[region_result.region_id]
        projected = [
            reproject_object(
                obj,
                region,
                base.image_width,
                base.image_height,
            )
            for obj in region_result.objects
            if obj.class_name != "line"
        ]
        received_objects = received_objects or bool(projected)
        if region.target_object_index is not None and projected:
            target = region.target_object_index
            if target < len(objects):
                same_class = [
                    obj for obj in projected
                    if obj.class_name == objects[target].class_name
                ]
                if same_class:
                    replacement = max(
                        same_class,
                        key=lambda obj: bbox_iou(objects[target], obj),
                    )
                    objects[target] = replacement
                    projected.remove(replacement)

        for candidate in projected:
            overlaps = [
                (index, bbox_iou(existing, candidate))
                for index, existing in enumerate(objects)
            ]
            same = [
                (index, score)
                for index, score in overlaps
                if objects[index].class_name == candidate.class_name
            ]
            if same and max(score for _, score in same) >= 0.60:
                index, _ = max(same, key=lambda pair: pair[1])
                objects[index] = candidate
                continue
            conflicting = [
                score
                for index, score in overlaps
                if objects[index].class_name != candidate.class_name
            ]
            if conflicting and max(conflicting) >= 0.70:
                conflicts = True
            objects.append(candidate)

    reasons = list(dict.fromkeys(response.review_reasons))
    if not received_objects:
        reasons = list(dict.fromkeys([*base.review_reasons, *reasons]))
    if conflicts and "detail_class_conflict" not in reasons:
        reasons.append("detail_class_conflict")
    return AgentAnnotation(
        image_width=base.image_width,
        image_height=base.image_height,
        objects=objects,
        needs_review=response.needs_review or conflicts or bool(reasons),
        review_reasons=reasons,
    )
