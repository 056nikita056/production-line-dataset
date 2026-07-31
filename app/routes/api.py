from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import ValidationError

from ..agent_schema import AgentAnnotation, parse_agent_json
from ..export_bundle import ExportError
from ..models import ItemStatus
from ..queue import InvalidTransition, ItemNotFound, QueueError
from ..settings import safe_resolve


router = APIRouter(prefix="/api")


def services(request: Request) -> Any:
    return request.app.state.services


def public_item(item: dict[str, Any]) -> dict[str, Any]:
    hidden = {
        "source_path",
        "result_path",
        "preview_path",
        "raw_response_path",
        "validation_path",
    }
    result = {key: value for key, value in item.items() if key not in hidden}
    item_id = item["id"]
    result["source_url"] = f"/media/items/{item_id}/source"
    result["preview_url"] = (
        f"/media/items/{item_id}/preview" if item.get("preview_path") else None
    )
    result["has_label"] = bool(item.get("result_path"))
    result["review_url"] = f"/review/{item_id}"
    return result


def item_with_annotation(request: Request, item: dict[str, Any]) -> dict[str, Any]:
    result = public_item(item)
    result["annotation"] = None
    raw_path = item.get("raw_response_path")
    if raw_path:
        try:
            path = safe_resolve(
                services(request).settings.root,
                services(request).settings.root / raw_path,
                must_exist=True,
            )
            result["annotation"] = parse_agent_json(
                path.read_text(encoding="utf-8")
            ).model_dump(mode="json")
            result["annotation"]["objects"] = [
                obj
                for obj in result["annotation"]["objects"]
                if obj["class_name"] != "line"
            ]
        except (OSError, ValueError, ValidationError):
            result["annotation"] = None
    return result


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    state = services(request)
    try:
        state.db.fetch_one("SELECT 1 AS ok")
        database = "ok"
    except Exception:
        database = "error"
    return {
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "worker_busy": state.executor_busy(),
        "version": "0.1.0",
    }


@router.get("/settings/public")
def public_settings(request: Request) -> dict[str, Any]:
    settings = services(request).settings
    return {
        "host": settings.app.host,
        "port": settings.app.port,
        "extensions": settings.input.extensions,
        "max_file_mb": settings.input.max_file_mb,
        "max_pixels": settings.input.max_pixels,
        "manual_review_required": settings.review.required_for_all_mvp_results,
        "classes": [
            {"id": 0, "name": "tray_filled"},
            {"id": 2, "name": "qr_code"},
            {"id": 3, "name": "tray_empty"},
        ],
    }


@router.post("/scan")
def scan(request: Request) -> dict[str, Any]:
    return services(request).scanner.scan().to_dict()


@router.post("/runs", status_code=202)
def create_run(request: Request) -> dict[str, Any]:
    state = services(request)
    ready, detail = state.agent_status()
    if not ready:
        raise HTTPException(
            status_code=503,
            detail=f"Codex не готов: {detail}. Выполните codex login и повторите.",
        )
    run = state.queue.create_run()
    if run["total_items"] == 0:
        state.queue.finalize_run(run["id"])
        raise HTTPException(status_code=409, detail="В очереди нет новых кадров")
    state.submit_run(run["id"])
    return state.queue.get_run(run["id"])


@router.get("/runs")
def list_runs(request: Request) -> list[dict[str, Any]]:
    return services(request).queue.list_runs()


@router.get("/runs/{run_id}")
def get_run(request: Request, run_id: str) -> dict[str, Any]:
    state = services(request)
    try:
        run = state.queue.get_run(run_id)
    except QueueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run["items"] = [
        public_item(item) for item in state.queue.list_items(run_id=run_id, limit=10_000)
    ]
    return run


@router.get("/items")
def list_items(
    request: Request,
    status: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    if status and status not in {value.value for value in ItemStatus}:
        raise HTTPException(status_code=422, detail="Неизвестный статус")
    return [
        public_item(item)
        for item in services(request).queue.list_items(
            status=status, run_id=run_id, limit=10_000
        )
    ]


@router.get("/items/{item_id}")
def get_item(request: Request, item_id: str) -> dict[str, Any]:
    try:
        item = services(request).queue.get_item(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return item_with_annotation(request, item)


@router.get("/items/{item_id}/next-review")
def get_next_review_item(request: Request, item_id: str) -> dict[str, Any]:
    try:
        item = services(request).queue.next_review_item(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"item": public_item(item) if item else None}


@router.post("/items/{item_id}/approve")
def approve_item(request: Request, item_id: str) -> dict[str, Any]:
    try:
        return public_item(services(request).queue.approve(item_id))
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (QueueError, InvalidTransition) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/items/{item_id}/reject")
def reject_item(request: Request, item_id: str) -> dict[str, Any]:
    try:
        return public_item(services(request).queue.reject(item_id))
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/items/{item_id}/retry", status_code=202)
def retry_item(request: Request, item_id: str) -> dict[str, Any]:
    state = services(request)
    try:
        item = state.queue.retry(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if item["status"] == "pending":
        run = state.queue.create_run()
        state.submit_run(run["id"])
        return {"item": public_item(state.queue.get_item(item_id)), "run": run}
    state.submit_item(item_id, item.get("run_id"))
    return {"item": public_item(item), "run": None}


@router.post("/items/{item_id}/annotation")
def correct_annotation(
    request: Request,
    item_id: str,
    annotation: AgentAnnotation,
) -> dict[str, Any]:
    state = services(request)
    try:
        item = state.worker.apply_annotation(item_id, annotation, manual=True)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, InvalidTransition, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return item_with_annotation(request, item)


@router.post("/exports", status_code=201)
def create_export(request: Request) -> dict[str, Any]:
    try:
        record = services(request).exports.create()
    except ExportError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": record["id"],
        "item_count": record["item_count"],
        "class_stats": record["class_stats"],
        "created_at": record["created_at"],
        "download_url": f"/api/exports/{record['id']}/download",
    }


@router.get("/exports/{export_id}/download")
def download_export(request: Request, export_id: str) -> FileResponse:
    try:
        path = services(request).exports.download_path(export_id)
    except ExportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    state = services(request)
    items = state.queue.list_items(limit=1000)
    counts = state.queue.status_counts()
    active_items = [
        item for item in items if item["status"] in {"pending", "processing"}
    ]
    inactive_items = [
        item for item in items if item["status"] not in {"pending", "processing"}
    ]
    return {
        "incoming": state.scanner.incoming_count(),
        "known": len(items),
        "counts": counts,
        "queued_count": counts["pending"] + counts["processing"],
        "runs": state.queue.list_runs(limit=8),
        "items": [
            public_item(item) for item in (active_items + inactive_items)[:50]
        ],
        "errors": state.db.recent_errors(8),
        "worker_busy": state.executor_busy(),
    }


@router.get("/exports")
def list_exports(request: Request) -> list[dict[str, Any]]:
    rows = services(request).db.fetch_all(
        "SELECT id,item_count,class_stats,created_at FROM exports ORDER BY created_at DESC LIMIT 50"
    )
    for row in rows:
        row["class_stats"] = json.loads(row["class_stats"])
        row["download_url"] = f"/api/exports/{row['id']}/download"
    return rows


@router.get("/items/{item_id}/validation")
def validation_report(request: Request, item_id: str) -> dict[str, Any]:
    try:
        item = services(request).queue.get_item(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not item.get("validation_path"):
        raise HTTPException(status_code=404, detail="Отчёт ещё не создан")
    path = safe_resolve(
        services(request).settings.root,
        services(request).settings.root / item["validation_path"],
        must_exist=True,
    )
    return json.loads(path.read_text(encoding="utf-8"))
