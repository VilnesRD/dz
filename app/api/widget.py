"""
Эндпоинты для виджета Битрикс24:
    GET/POST /bitrix/widget     — HTML виджета (iframe в карточке сделки)
    GET      /api/widget-config — список активных шаблонов для <select>
    GET/POST /install           — установка: сохраняем токены + регистрируем кнопку
"""
from pathlib import Path
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db import repository as repo

logger = logging.getLogger(__name__)
router = APIRouter()
STATIC_DIR = Path(__file__).parent.parent / "static"
settings = get_settings()


@router.get("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
@router.post("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget(request: Request):
    """Б24 открывает эту страницу в iframe при нажатии кнопки."""
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html")


@router.get("/api/widget-config")
async def widget_config():
    """Список активных шаблонов для выпадающего списка в виджете."""
    with SessionLocal() as db:
        templates = repo.list_templates(db)
    active = [{"key": t.key, "name": t.name} for t in templates if t.active]
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
async def install_get(request: Request):
    """GET /install — просто показываем страницу."""
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")


@router.post("/install", response_class=FileResponse, include_in_schema=False)
async def install_post(request: Request):
    """
    POST /install — Б24 шлёт сюда токены при установке локального приложения.

    Тело запроса содержит:
        AUTH_ID      — access token
        REFRESH_ID   — refresh token
        AUTH_EXPIRES — время жизни токена в секундах
        member_id    — ID портала
        DOMAIN       — домен портала (в query string)
    """
    params = dict(request.query_params)
    form   = dict(await request.form())

    logger.info("install POST query_params: %s", params)
    logger.info("install POST form body: %s", {
        k: v for k, v in form.items() if "ID" not in k  # не логируем токены
    })

    domain     = params.get("DOMAIN") or params.get("domain") or form.get("DOMAIN") or form.get("domain")
    auth_id    = form.get("AUTH_ID")
    refresh_id = form.get("REFRESH_ID")
    expires    = int(form.get("AUTH_EXPIRES", 3600))
    member_id  = form.get("member_id")

    logger.info("install POST: domain=%s auth=%s", domain, bool(auth_id))

    if auth_id and domain:
        try:
            # ── Сохраняем токены в БД ─────────────────────────────────────────
            with SessionLocal() as db:
                repo.save_oauth_token(db, domain, auth_id, refresh_id, expires, member_id)
            logger.info("✅ Токены сохранены для %s", domain)

            # ── Регистрируем кнопку в CRM ─────────────────────────────────────
            widget_url = f"{settings.APP_PUBLIC_URL}/bitrix/widget"
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://{domain}/rest/placement.bind.json",
                    params={"auth": auth_id},
                    json={
                        "PLACEMENT": "CRM_DEAL_DETAIL_TOOLBAR",
                        "HANDLER":   widget_url,
                        "TITLE":     "📄 Создать документ",
                    }
                )
                result = r.json()
                logger.info("placement.bind: %s", result)

        except Exception as e:
            logger.error("Ошибка при установке: %s", e)
    else:
        logger.warning("install POST: нет AUTH_ID или domain — пропускаем")

    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")
