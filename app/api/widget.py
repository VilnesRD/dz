"""
Эндпоинты для виджета Битрикс24:
    GET/POST /bitrix/widget  — HTML виджета (iframe в карточке сделки)
    GET /api/widget-config   — список активных шаблонов для <select>
    GET/POST /install        — страница установки Б24-приложения
"""
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from app.db.database import SessionLocal
from app.db import repository as repo

router = APIRouter()
STATIC_DIR = Path(__file__).parent.parent / "static"


@router.get("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
@router.post("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget(request: Request):
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html")


@router.get("/api/widget-config")
async def widget_config():
    with SessionLocal() as db:
        templates = repo.list_templates(db)
    active = [{"key": t.key, "name": t.name} for t in templates if t.active]
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
@router.post("/install", response_class=FileResponse, include_in_schema=False)
async def install_page(request: Request):
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")