from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ReviewReason = Literal[
    "blurred_object",
    "uncertain_tray_state",
    "uncertain_object_boundary",
    "heavy_occlusion",
    "camera_or_line_shift",
    "ambiguous_qr_code",
    "agent_output_invalid",
    "agent_timeout",
    "agent_failure",
    "detail_class_conflict",
]

ClassName = Literal["tray_filled", "line", "qr_code", "tray_empty"]


class StrictAgentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Point(StrictAgentModel):
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)


class AnnotationObject(StrictAgentModel):
    class_id: int = Field(ge=0, le=3)
    class_name: ClassName
    polygon: list[Point] = Field(min_length=4, max_length=20)
    occluded: bool
    visible_fraction: float = Field(ge=0, le=1)


class AgentAnnotation(StrictAgentModel):
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    objects: list[AnnotationObject]
    needs_review: bool
    review_reasons: list[ReviewReason]


class DetailRegionResult(StrictAgentModel):
    region_id: str = Field(min_length=1, max_length=80)
    objects: list[AnnotationObject]


class DetailAgentResponse(StrictAgentModel):
    regions: list[DetailRegionResult] = Field(min_length=1, max_length=4)
    needs_review: bool
    review_reasons: list[ReviewReason]


def parse_agent_json(raw: str) -> AgentAnnotation:
    return AgentAnnotation.model_validate_json(raw)


def parse_detail_json(raw: str) -> DetailAgentResponse:
    return DetailAgentResponse.model_validate_json(raw)
