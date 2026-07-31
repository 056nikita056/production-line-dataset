from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from ..queue import ItemNotFound
from ..settings import safe_resolve


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={},
    )


@router.get("/review/{item_id}", response_class=HTMLResponse)
def review(request: Request, item_id: str) -> HTMLResponse:
    try:
        item = request.app.state.services.queue.get_item(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return request.app.state.templates.TemplateResponse(
        request=request,
        name="review.html",
        context={"item_id": item_id, "item_name": item["original_name"]},
    )


@router.get("/media/items/{item_id}/{kind}")
def item_media(request: Request, item_id: str, kind: str) -> FileResponse:
    try:
        item = request.app.state.services.queue.get_item(item_id)
    except ItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    key = {"source": "source_path", "preview": "preview_path"}.get(kind)
    if not key or not item.get(key):
        raise HTTPException(status_code=404, detail="Файл не найден")
    settings = request.app.state.services.settings
    path = safe_resolve(
        settings.root,
        settings.root / item[key],
        must_exist=True,
    )
    return FileResponse(path)

