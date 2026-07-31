from __future__ import annotations

from pathlib import Path

from .agent_schema import AgentAnnotation, AnnotationObject


TRAINING_CLASS_IDS = {
    "tray_filled": 0,
    "qr_code": 1,
    "tray_empty": 2,
}


def _center_y(obj: AnnotationObject) -> float:
    return sum(point.y for point in obj.polygon) / len(obj.polygon)


def sorted_objects(annotation: AgentAnnotation) -> list[AnnotationObject]:
    def key(obj: AnnotationObject) -> tuple[float, float, float, str]:
        if obj.class_name == "line":
            group = 0.0
        elif obj.class_name in {"tray_filled", "tray_empty"}:
            group = 1.0
        else:
            group = 2.0
        center_x = sum(point.x for point in obj.polygon) / len(obj.polygon)
        return group, _center_y(obj), center_x, obj.class_name

    return sorted(annotation.objects, key=key)


def yolo_lines(annotation: AgentAnnotation, coordinate_scale: int = 1000) -> list[str]:
    lines: list[str] = []
    for obj in sorted_objects(annotation):
        if obj.class_name == "line":
            continue
        coordinates = [
            f"{value / coordinate_scale:.6f}"
            for point in obj.polygon
            for value in (point.x, point.y)
        ]
        lines.append(f"{TRAINING_CLASS_IDS[obj.class_name]} {' '.join(coordinates)}")
    return lines


def write_yolo(
    annotation: AgentAnnotation,
    path: Path,
    coordinate_scale: int = 1000,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = yolo_lines(annotation, coordinate_scale)
    content = "\n".join(lines)
    if lines:
        content += "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return path
