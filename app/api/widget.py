"""
Эндпоинты для виджета Битрикс24:
    GET /bitrix/widget      — HTML виджета (iframe в карточке сделки)
    GET /api/widget-config  — список активных шаблонов для <select>
    GET /install            — страница установки Б24-приложения
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.db.database import SessionLocal
from app.db import repository as repo

router = APIRouter()
STATIC_DIR = Path(__file__).parent.parent / "static"


@router.get("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget():
    """
    HTML-страница виджета. Этот URL прописывается в placement.bind при регистрации
    кнопки в Б24. Б24 открывает его в iframe, передавая DEAL_ID, AUTH_ID и др.
    """
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html")


@router.get("/api/widget-config")
async def widget_config():
    """
    Конфиг для виджета: список активных шаблонов для выпадающего списка.
    Виджет вызывает этот эндпоинт при загрузке.
    """
    with SessionLocal() as db:
        templates = repo.list_templates(db)

    active = [
        {"key": t.key, "name": t.name}
        for t in templates
        if t.active
    ]
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
async def install_page():
    """
    Страница установки локального приложения Б24.
    Вызывает BX24.installFinish() — сигнал об успешной установке.
    Прописывается в поле «URL для установки» в настройках Б24-приложения.
    """
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")
