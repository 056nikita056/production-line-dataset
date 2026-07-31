from __future__ import annotations

import math
from collections import Counter

from .agent_schema import AgentAnnotation, AnnotationObject, Point
from .models import CLASS_NAMES, ValidationResult
from .settings import Settings


def polygon_area(points: list[Point]) -> float:
    return abs(
        sum(
            points[index].x * points[(index + 1) % len(points)].y
            - points[(index + 1) % len(points)].x * points[index].y
            for index in range(len(points))
        )
        / 2.0
    )


def bbox(obj: AnnotationObject) -> tuple[float, float, float, float]:
    xs = [point.x for point in obj.polygon]
    ys = [point.y for point in obj.polygon]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_iou(first: AnnotationObject, second: AnnotationObject) -> float:
    ax1, ay1, ax2, ay2 = bbox(first)
    bx1, by1, bx2, by2 = bbox(second)
    intersection = max(0.0, min(ax2, bx2) - max(ax1, bx1)) * max(
        0.0, min(ay2, by2) - max(ay1, by1)
    )
    first_area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    second_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = first_area + second_area - intersection
    return intersection / union if union else 0.0


def _distance(a: Point, b: Point) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def _orientation(first: Point, second: Point, third: Point) -> int:
    value = (
        (second.y - first.y) * (third.x - second.x)
        - (second.x - first.x) * (third.y - second.y)
    )
    if value == 0:
        return 0
    return 1 if value > 0 else 2


def _on_segment(first: Point, second: Point, third: Point) -> bool:
    return (
        min(first.x, third.x) <= second.x <= max(first.x, third.x)
        and min(first.y, third.y) <= second.y <= max(first.y, third.y)
    )


def _segments_intersect(
    first: Point,
    second: Point,
    third: Point,
    fourth: Point,
) -> bool:
    o1 = _orientation(first, second, third)
    o2 = _orientation(first, second, fourth)
    o3 = _orientation(third, fourth, first)
    o4 = _orientation(third, fourth, second)
    if o1 != o2 and o3 != o4:
        return True
    return (
        (o1 == 0 and _on_segment(first, third, second))
        or (o2 == 0 and _on_segment(first, fourth, second))
        or (o3 == 0 and _on_segment(third, first, fourth))
        or (o4 == 0 and _on_segment(third, second, fourth))
    )


def polygon_self_intersects(points: list[Point]) -> bool:
    size = len(points)
    for first_index in range(size):
        first_next = (first_index + 1) % size
        for second_index in range(first_index + 1, size):
            second_next = (second_index + 1) % size
            if (
                first_index == second_index
                or first_next == second_index
                or second_next == first_index
            ):
                continue
            if _segments_intersect(
                points[first_index],
                points[first_next],
                points[second_index],
                points[second_next],
            ):
                return True
    return False


def is_rectangle(points: list[Point], tolerance: float) -> bool:
    if len(points) != 4 or len({(point.x, point.y) for point in points}) != 4:
        return False
    area = polygon_area(points)
    if area <= 1:
        return False

    vectors = [
        (
            points[(index + 1) % 4].x - points[index].x,
            points[(index + 1) % 4].y - points[index].y,
        )
        for index in range(4)
    ]
    lengths = [math.hypot(x, y) for x, y in vectors]
    if min(lengths) <= 1:
        return False

    # Рамка прямоугольного объекта в перспективе становится трапецией.
    # Требуем выпуклый четырёхугольник с последовательными вершинами,
    # разумными углами и достаточным заполнением собственного bbox.
    crosses = [
        vectors[index][0] * vectors[(index + 1) % 4][1]
        - vectors[index][1] * vectors[(index + 1) % 4][0]
        for index in range(4)
    ]
    if any(abs(cross) <= 1 for cross in crosses):
        return False
    if not (all(cross > 0 for cross in crosses) or all(cross < 0 for cross in crosses)):
        return False

    minimum_angle = max(20.0, 45.0 - tolerance * 100.0)
    for index in range(4):
        previous = (
            points[(index - 1) % 4].x - points[index].x,
            points[(index - 1) % 4].y - points[index].y,
        )
        following = (
            points[(index + 1) % 4].x - points[index].x,
            points[(index + 1) % 4].y - points[index].y,
        )
        denominator = math.hypot(*previous) * math.hypot(*following)
        cosine = max(
            -1.0,
            min(1.0, (previous[0] * following[0] + previous[1] * following[1]) / denominator),
        )
        angle = math.degrees(math.acos(cosine))
        if angle < minimum_angle or angle > 180.0 - minimum_angle:
            return False

    xs = [point.x for point in points]
    ys = [point.y for point in points]
    bounding_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    minimum_fill = max(0.30, 0.65 - tolerance)
    return bounding_area > 0 and area / bounding_area >= minimum_fill


def _line_template_shift(obj: AnnotationObject, template: list[list[int]]) -> float:
    if not template or len(obj.polygon) != len(template):
        return float("inf")
    direct = sum(
        math.hypot(point.x - target[0], point.y - target[1])
        for point, target in zip(obj.polygon, template, strict=True)
    ) / len(template)
    reverse = sum(
        math.hypot(point.x - target[0], point.y - target[1])
        for point, target in zip(reversed(obj.polygon), template, strict=True)
    ) / len(template)
    return min(direct, reverse)


def validate_annotation(
    annotation: AgentAnnotation,
    settings: Settings,
    *,
    expected_width: int | None = None,
    expected_height: int | None = None,
    manual: bool = False,
) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    if expected_width is not None and annotation.image_width != expected_width:
        errors.append("image_width_mismatch")
    if expected_height is not None and annotation.image_height != expected_height:
        errors.append("image_height_mismatch")
    if annotation.needs_review and not annotation.review_reasons:
        errors.append("review_reasons_required")
    if not annotation.needs_review and annotation.review_reasons:
        errors.append("needs_review_flag_required")
    if (
        "uncertain_tray_state" in annotation.review_reasons
        and not annotation.needs_review
    ):
        errors.append("uncertain_tray_requires_review")
    reason_counts = Counter(annotation.review_reasons)
    if any(count > 1 for count in reason_counts.values()):
        errors.append("duplicate_review_reason")

    signatures: set[tuple[object, ...]] = set()
    line_objects = [obj for obj in annotation.objects if obj.class_name == "line"]
    if (
        settings.annotation.line_use_camera_template
        and settings.annotation.line_expected_count is not None
        and len(line_objects) != settings.annotation.line_expected_count
    ):
        errors.append("line_count_mismatch")
    for index, obj in enumerate(annotation.objects):
        prefix = f"object_{index}"
        if CLASS_NAMES.get(obj.class_id) != obj.class_name:
            errors.append(f"{prefix}:class_id_name_mismatch")
        required_points = 4 if obj.class_name != "line" and not manual else None
        if required_points and len(obj.polygon) != required_points:
            errors.append(f"{prefix}:requires_four_points")
        if manual and obj.class_name == "line":
            errors.append(f"{prefix}:manual_line_forbidden")
        if manual and not 4 <= len(obj.polygon) <= 20:
            errors.append(f"{prefix}:manual_point_count")
        if any(
            obj.polygon[point_index]
            == obj.polygon[(point_index + 1) % len(obj.polygon)]
            for point_index in range(len(obj.polygon))
        ):
            errors.append(f"{prefix}:adjacent_duplicate_point")
        if polygon_self_intersects(obj.polygon):
            errors.append(f"{prefix}:self_intersection")
        if obj.class_name == "line" and not 4 <= len(obj.polygon) <= 20:
            errors.append(f"{prefix}:line_point_count")
        if polygon_area(obj.polygon) <= 1:
            errors.append(f"{prefix}:zero_area")
        if obj.class_name != "line" and not manual and not is_rectangle(
            obj.polygon, settings.annotation.rectangle_tolerance
        ):
            errors.append(f"{prefix}:not_rectangle")
        signature = (
            obj.class_id,
            tuple((point.x, point.y) for point in obj.polygon),
        )
        if signature in signatures:
            errors.append(f"{prefix}:exact_duplicate")
        signatures.add(signature)
        if obj.class_name == "qr_code":
            if obj.occluded:
                errors.append(f"{prefix}:qr_occluded")
            if obj.visible_fraction < 1.0:
                errors.append(f"{prefix}:qr_not_fully_visible")
        if obj.class_name in {"tray_filled", "tray_empty"}:
            if obj.visible_fraction < settings.annotation.tray_min_visible_fraction:
                errors.append(f"{prefix}:tray_below_min_visibility")
            if obj.occluded and obj.visible_fraction < 0.5:
                warnings.append(f"{prefix}:heavy_occlusion_review")
        if (
            obj.class_name == "line"
            and settings.annotation.line_use_camera_template
        ):
            shift = _line_template_shift(obj, settings.annotation.line_template)
            if shift > settings.annotation.line_shift_tolerance:
                warnings.append(f"{prefix}:camera_or_line_shift")
                if not annotation.needs_review or "camera_or_line_shift" not in annotation.review_reasons:
                    errors.append(f"{prefix}:line_shift_requires_review")

    filled = [obj for obj in annotation.objects if obj.class_name == "tray_filled"]
    empty = [obj for obj in annotation.objects if obj.class_name == "tray_empty"]
    for filled_obj in filled:
        for empty_obj in empty:
            if bbox_iou(filled_obj, empty_obj) >= settings.annotation.conflict_iou_threshold:
                errors.append("tray_state_conflict")
                if (
                    not annotation.needs_review
                    or "uncertain_tray_state" not in annotation.review_reasons
                ):
                    errors.append("tray_state_conflict_requires_review")
    return ValidationResult(valid=not errors, errors=sorted(set(errors)), warnings=sorted(set(warnings)))
