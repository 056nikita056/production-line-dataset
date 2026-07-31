from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .agent_schema import AgentAnnotation


COLORS: dict[str, tuple[int, int, int]] = {
    "tray_filled": (239, 91, 74),
    "line": (45, 167, 190),
    "qr_code": (246, 190, 64),
    "tray_empty": (83, 188, 117),
}


def create_preview(
    source: Path,
    annotation: AgentAnnotation,
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = original.convert("RGB")
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = ImageFont.load_default()
    for index, obj in enumerate(annotation.objects, start=1):
        if obj.class_name == "line":
            continue
        color = COLORS[obj.class_name]
        points = [
            (round(point.x / 1000 * width), round(point.y / 1000 * height))
            for point in obj.polygon
        ]
        draw.polygon(points, fill=(*color, 45))
        draw.line(points + [points[0]], fill=(*color, 255), width=max(2, width // 500))
        label = f"{index}. {obj.class_name}"
        if annotation.needs_review:
            label += " · REVIEW"
        left = min(point[0] for point in points)
        top = max(0, min(point[1] for point in points) - 18)
        text_box = draw.textbbox((left, top), label, font=font)
        draw.rectangle(
            (text_box[0] - 3, text_box[1] - 2, text_box[2] + 3, text_box[3] + 2),
            fill=(*color, 235),
        )
        draw.text((left, top), label, fill=(15, 23, 30, 255), font=font)
    result = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    result.save(temporary, format="JPEG", quality=92, optimize=True)
    temporary.replace(destination)
    return destination
