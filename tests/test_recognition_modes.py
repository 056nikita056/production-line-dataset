from __future__ import annotations

import copy

from app.codex_runner import FakeCodexRunner
from app.queue import QueueRepository
from app.scanner import Scanner
from app.worker import Worker


def _payload_with_reason(payload, reason: str):
    changed = copy.deepcopy(payload)
    changed["needs_review"] = True
    changed["review_reasons"] = [reason]
    return changed


def _process_one(
    settings,
    db,
    queue: QueueRepository,
    image_factory,
    runner,
    *,
    mode: str,
):
    image_factory(settings.path("incoming") / "mode.jpg", (160, 90))
    assert Scanner(settings, db, queue).scan().added == 1
    run = queue.create_run(mode)
    finished = Worker(settings, db, queue, runner).run(run["id"])
    return queue.list_items()[0], finished


def test_1x_calls_runner_exactly_once(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload)
    item, run = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="single",
    )

    assert runner.calls == 1
    assert run["recognition_mode"] == "single"
    assert run["max_auto_attempts"] == 1
    assert run["codex_call_count"] == 1
    assert item["status"] == "review"


def test_good_result_in_2x_does_not_spend_second_call(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload)
    _, run = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    assert runner.calls == 1
    assert run["max_auto_attempts"] == 2
    assert run["codex_call_count"] == 1


def test_retryable_result_in_2x_calls_runner_twice_and_selects_better(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    first = copy.deepcopy(valid_payload)
    first["objects"][0]["polygon"][2] = {"x": 200, "y": 300}
    runner = FakeCodexRunner([first, valid_payload])
    item, run = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    attempts = queue.list_attempts(item["id"])
    assert runner.calls == 2
    assert run["codex_call_count"] == 2
    assert runner.retry_contexts == [None, "not_rectangle"]
    assert len(attempts) == 2
    assert all(attempt["revision_id"] for attempt in attempts)
    assert attempts[0]["attempt_kind"] == "auto_retry"
    assert attempts[0]["selected"]
    assert attempts[0]["selection_reason"] == "second_is_valid"
    assert attempts[1]["validation_errors"] == ["object_0:not_rectangle"]


def test_non_retryable_reason_in_2x_stops_after_first_call(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    payload = _payload_with_reason(valid_payload, "uncertain_tray_state")
    runner = FakeCodexRunner(payload)
    item, _ = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    assert runner.calls == 1
    assert len(queue.list_attempts(item["id"])) == 1


def test_invalid_json_in_2x_gets_one_automatic_retry(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(["not-json", valid_payload])
    item, _ = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    attempts = queue.list_attempts(item["id"])
    assert runner.calls == 2
    assert attempts[0]["selected"]
    assert attempts[1]["validation_errors"] == ["agent_output_invalid"]
    assert runner.retry_contexts == [None, "agent_output_invalid"]


def test_technical_failure_uses_second_and_final_call_in_2x(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload, exit_code=[9, 0])
    item, _ = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    attempts = queue.list_attempts(item["id"])
    assert runner.calls == 2
    assert attempts[0]["attempt_kind"] == "technical_retry"
    assert attempts[0]["selected"]
    assert attempts[1]["validation_errors"] == ["agent_failure"]


def test_third_automatic_call_is_impossible(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    retryable = _payload_with_reason(valid_payload, "blurred_object")
    runner = FakeCodexRunner([retryable, retryable, retryable])
    item, run = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    assert runner.calls == 2
    assert run["codex_call_count"] == 2
    assert len(queue.list_attempts(item["id"])) == 2


def test_manual_retry_starts_new_cycle(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    runner = FakeCodexRunner(valid_payload)
    item, _ = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="single",
    )
    retried = queue.retry(item["id"], "auto_retry")
    worker = Worker(settings, db, queue, runner)
    worker.process_claimed(item["id"])
    queue.finalize_run(retried["run_id"])

    attempts = queue.list_attempts(item["id"])
    assert runner.calls == 2
    assert [attempt["cycle_no"] for attempt in attempts] == [2, 1]
    assert attempts[0]["attempt_kind"] == "manual_retry"
    assert len(queue.list_revisions(item["id"])) == 2


def test_valid_disagreement_keeps_first_and_requires_human_choice(
    settings,
    db,
    queue,
    image_factory,
    valid_payload,
):
    first = _payload_with_reason(valid_payload, "blurred_object")
    second = copy.deepcopy(valid_payload)
    second["objects"][0]["class_id"] = 3
    second["objects"][0]["class_name"] = "tray_empty"
    runner = FakeCodexRunner([first, second])
    item, _ = _process_one(
        settings,
        db,
        queue,
        image_factory,
        runner,
        mode="auto_retry",
    )

    attempts = queue.list_attempts(item["id"])
    assert runner.calls == 2
    assert attempts[1]["selected"]
    assert attempts[1]["selection_reason"] == "valid_results_disagree"
    assert "attempts_disagree" in item["review_reasons"]
