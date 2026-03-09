"""
Эндпоинты для виджета Битрикс24:
    GET/POST /bitrix/widget  — HTML виджета (iframe в карточке сделки)
    GET      /api/widget-config — список активных шаблонов для <select>
    GET/POST /install        — OAuth-установка: обмен code→токены + placement.bind
"""
from datetime import datetime
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
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html")


@router.get("/api/widget-config")
async def widget_config():
    with SessionLocal() as db:
        templates = repo.list_templates(db)
    active = [{"key": t.key, "name": t.name} for t in templates if t.active]
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
async def install_get(request: Request):
    """GET /install — просто показываем страницу."""
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")


@router.post("/install", response_class=HTMLResponse, include_in_schema=False)
async def install_post(request: Request):
    """
    POST /install — Б24 шлёт сюда code при установке.
    Обмениваем code на access_token + refresh_token, сохраняем в БД,
    регистрируем placement CRM_DEAL_TOOLBAR.
    """
    params = dict(request.query_params)
    form   = dict(await request.form())
    logger.info("install POST query_params: %s", params)
    logger.info("install POST form body: %s", form)
    code   = params.get("code") or form.get("code")
    domain = params.get("DOMAIN") or params.get("domain") or form.get("DOMAIN") or form.get("domain")

    logger.info("install POST: domain=%s code=%s", domain, bool(code))

    if code and domain and settings.BITRIX_CLIENT_ID and settings.BITRIX_CLIENT_SECRET:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # ── Обмен code → токены ───────────────────────────────────────
                r = await client.get("https://oauth.bitrix.info/oauth/token/", params={
                    "grant_type":    "authorization_code",
                    "client_id":     settings.BITRIX_CLIENT_ID,
                    "client_secret": settings.BITRIX_CLIENT_SECRET,
                    "code":          code,
                })
                token_data = r.json()
                logger.info("token response: %s", token_data)

                if "access_token" in token_data:
                    access_token  = token_data["access_token"]
                    refresh_token = token_data["refresh_token"]
                    expires_in    = int(token_data.get("expires_in", 3600))
                    member_id     = token_data.get("member_id")

                    # Сохраняем токены в БД
                    with SessionLocal() as db:
                        repo.save_oauth_token(db, domain, access_token,
                                              refresh_token, expires_in, member_id)
                    logger.info("✅ Токены сохранены для %s", domain)

                    # ── Регистрируем кнопку в CRM ─────────────────────────────
                    widget_url = f"{settings.APP_PUBLIC_URL}/bitrix/widget"
                    r2 = await client.post(
                        f"https://{domain}/rest/placement.bind.json",
                        params={"auth": access_token},
                        json={
                            "PLACEMENT": "CRM_DEAL_TOOLBAR",
                            "HANDLER":   widget_url,
                            "TITLE":     "📄 Создать документ",
                        }
                    )
                    logger.info("placement.bind: %s", r2.json())
                else:
                    logger.error("Ошибка получения токена: %s", token_data)

        except Exception as e:
            logger.error("Ошибка при установке: %s", e)

    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")