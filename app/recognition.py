from __future__ import annotations

from dataclasses import dataclass

from .agent_schema import AgentAnnotation
from .models import ValidationResult


RETRYABLE_REVIEW_REASONS = {
    "blurred_object",
    "uncertain_object_boundary",
}

NON_RETRYABLE_REVIEW_REASONS = {
    "uncertain_tray_state",
    "heavy_occlusion",
    "ambiguous_qr_code",
}

RETRYABLE_ERROR_MARKERS = {
    "agent_failure",
    "agent_output_invalid",
    "agent_timeout",
    "class_id_name_mismatch",
    "exact_duplicate",
    "image_height_mismatch",
    "image_width_mismatch",
    "line_count_mismatch",
    "line_point_count",
    "not_rectangle",
    "requires_four_points",
    "zero_area",
}


@dataclass(frozen=True, slots=True)
class RecognitionCandidate:
    revision_id: str
    attempt_id: int
    annotation: AgentAnnotation
    validation: ValidationResult
    detail: bool = False


def retry_reason(
    *,
    technical_reason: str | None = None,
    validation: ValidationResult | None = None,
    annotation: AgentAnnotation | None = None,
) -> str | None:
    if technical_reason in {
        "agent_failure",
        "agent_output_invalid",
        "agent_timeout",
        "missing_agent_response",
    }:
        return technical_reason
    if annotation:
        reasons = set(annotation.review_reasons)
        non_retryable = reasons & NON_RETRYABLE_REVIEW_REASONS
        if non_retryable:
            return None
    if validation:
        for error in validation.errors:
            marker = error.rsplit(":", 1)[-1]
            if marker in RETRYABLE_ERROR_MARKERS:
                return marker
    if annotation:
        for reason in annotation.review_reasons:
            if reason in RETRYABLE_REVIEW_REASONS:
                return reason
    return None


def _object_signatures(annotation: AgentAnnotation) -> set[tuple[object, ...]]:
    return {
        (
            obj.class_name,
            tuple((point.x, point.y) for point in obj.polygon),
            obj.occluded,
            obj.visible_fraction,
        )
        for obj in annotation.objects
        if obj.class_name != "line"
    }


def choose_candidate(
    candidates: list[RecognitionCandidate],
) -> tuple[RecognitionCandidate, str]:
    if not candidates:
        raise ValueError("Нет результатов распознавания для выбора")
    if len(candidates) == 1:
        return candidates[0], "only_available_result"

    first, second = candidates[0], candidates[1]
    if (
        second.detail
        and "detail_class_conflict" in second.annotation.review_reasons
    ):
        return first, "detail_class_conflict"
    if first.validation.valid != second.validation.valid:
        return (
            (first, "first_is_valid")
            if first.validation.valid
            else (second, "second_is_valid")
        )
    if len(first.validation.errors) != len(second.validation.errors):
        return (
            (first, "first_has_fewer_validation_errors")
            if len(first.validation.errors) < len(second.validation.errors)
            else (second, "second_has_fewer_validation_errors")
        )

    if (
        second.detail
        and second.validation.valid
        and "detail_class_conflict" not in second.annotation.review_reasons
        and len(second.annotation.review_reasons)
        <= len(first.annotation.review_reasons)
    ):
        return second, "detail_refinement_applied"

    first_objects = _object_signatures(first.annotation)
    second_objects = _object_signatures(second.annotation)
    if first.validation.valid and first_objects != second_objects:
        return first, "valid_results_disagree"
    if (
        len(second.annotation.review_reasons)
        < len(first.annotation.review_reasons)
        and len(second_objects) >= len(first_objects)
    ):
        return second, "second_has_fewer_review_reasons"
    return first, "first_kept_by_deterministic_tie_break"
