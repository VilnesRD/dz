"""
Эндпоинты для виджета Битрикс24:
    GET/POST /bitrix/widget     — HTML виджета (iframe в карточке сделки)
    GET      /api/widget-config — список активных шаблонов для <select>
    GET/POST /install           — установка: сохраняем токены + регистрируем кнопку
"""
from pathlib import Path
import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db import repository as repo

logger = logging.getLogger(__name__)
router = APIRouter()
STATIC_DIR = Path(__file__).parent.parent / "static"
settings = get_settings()


def _pick(*values):
    for value in values:
        if value:
            return str(value)
    return None


def _normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    domain = value.strip().replace("https://", "").replace("http://", "")
    domain = domain.split("/", 1)[0]
    return domain or None


def _safe_int(value, default: int = 3600) -> int:
    try:
        return max(int(str(value)), 120)
    except Exception:
        return default


async def _process_install_payload(params: dict, form: dict) -> None:
    domain = _normalize_domain(_pick(
        params.get("DOMAIN"),
        params.get("domain"),
        form.get("DOMAIN"),
        form.get("domain"),
        form.get("auth[domain]"),
        params.get("auth[domain]"),
    ))
    auth_id = _pick(
        form.get("AUTH_ID"),
        params.get("AUTH_ID"),
        form.get("auth[access_token]"),
        params.get("auth[access_token]"),
        form.get("AUTH"),
        params.get("AUTH"),
    )
    refresh_id = _pick(
        form.get("REFRESH_ID"),
        params.get("REFRESH_ID"),
        form.get("auth[refresh_token]"),
        params.get("auth[refresh_token]"),
    )
    expires = _safe_int(_pick(
        form.get("AUTH_EXPIRES"),
        params.get("AUTH_EXPIRES"),
        form.get("auth[expires]"),
        params.get("auth[expires]"),
    ))
    member_id = _pick(
        form.get("member_id"),
        params.get("member_id"),
        form.get("auth[member_id]"),
        params.get("auth[member_id]"),
    )

    logger.info("install: domain=%s auth=%s", domain, bool(auth_id))

    if not (auth_id and domain):
        logger.warning("install: нет AUTH_ID или domain — пропускаем")
        return

    with SessionLocal() as db:
        repo.save_oauth_token(db, domain, auth_id, refresh_id or "", expires, member_id)
    logger.info("✅ Токены сохранены для %s", domain)

    widget_url = f"{settings.APP_PUBLIC_URL}/bitrix/widget"
    async with httpx.AsyncClient(timeout=15) as client:
        # Сначала удаляем старые привязки этого приложения, чтобы гарантированно
        # обновить handler после переустановки/смены URL.
        list_resp = await client.post(
            f"https://{domain}/rest/placement.list.json",
            params={"auth": auth_id},
        )
        try:
            placements = list_resp.json().get("result", [])
        except Exception:
            placements = []

        target_placements = {"CRM_DEAL_TOOLBAR", "CRM_DEAL_DETAIL_TOOLBAR"}
        for item in placements:
            placement_name = str(item.get("placement") or "")
            handler = str(item.get("handler") or "")
            if placement_name not in target_placements:
                continue

            unbind_resp = await client.post(
                f"https://{domain}/rest/placement.unbind.json",
                params={"auth": auth_id},
                json={
                    "PLACEMENT": placement_name,
                    "HANDLER": handler,
                },
            )
            try:
                unbind_data = unbind_resp.json()
            except Exception:
                unbind_data = {"status_code": unbind_resp.status_code}
            logger.info("placement.unbind %s (%s): %s", placement_name, handler, unbind_data)

        # Главный placement для этого портала
        bind_targets = ("CRM_DEAL_DETAIL_TOOLBAR", "CRM_DEAL_TOOLBAR")
        for placement in bind_targets:
            r = await client.post(
                f"https://{domain}/rest/placement.bind.json",
                params={"auth": auth_id},
                json={
                    "PLACEMENT": placement,
                    "HANDLER": widget_url,
                    "TITLE": "📄 Создать документ",
                    "DESCRIPTION": "Генерация PDF через Doczilla PRO",
                }
            )
            try:
                data = r.json()
            except Exception:
                data = {"status_code": r.status_code, "text": r.text[:500]}
            logger.info("placement.bind %s: %s", placement, data)


@router.get("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget_get(request: Request):
    """GET — Б24 открывает iframe."""
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html")


@router.post("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget_post(request: Request):
    """
    POST — Б24 может открыть handler через form-data.
    Преобразуем в redirect на GET + query params, чтобы JS в iframe получил контекст.
    """
    form = dict(await request.form())
    logger.info("widget POST form keys: %s", sorted(form.keys()))

    deal_id = _pick(
        form.get("DEAL_ID"),
        form.get("deal_id"),
        form.get("ID"),
        form.get("ENTITY_ID"),
        form.get("ENTITY_VALUE_ID"),
    )
    domain = _normalize_domain(_pick(
        form.get("DOMAIN"),
        form.get("domain"),
        form.get("auth[domain]"),
    ))
    placement_options = _pick(
        form.get("PLACEMENT_OPTIONS"),
        form.get("placement_options"),
    )
    template_key = _pick(form.get("template_key"), form.get("TEMPLATE_KEY"))

    qs = {}
    if deal_id:
        qs["DEAL_ID"] = deal_id
    if domain:
        qs["DOMAIN"] = domain
    if placement_options:
        qs["PLACEMENT_OPTIONS"] = placement_options
    if template_key:
        qs["TEMPLATE_KEY"] = template_key

    target = str(request.url_for("serve_widget_get"))
    if qs:
        target = f"{target}?{urlencode(qs)}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/api/widget-config")
async def widget_config():
    """Список активных шаблонов для выпадающего списка в виджете."""
    with SessionLocal() as db:
        templates = repo.list_templates(db)
    active = [{"key": t.key, "name": t.name} for t in templates if t.active]
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
async def install_get(request: Request):
    """
    GET /install — страница установки.
    Некоторые порталы Б24 передают auth-параметры именно в query-string на GET.
    """
    params = dict(request.query_params)
    if params:
        logger.info("install GET query_params: %s", params)
        try:
            await _process_install_payload(params, {})
        except Exception as e:
            logger.error("Ошибка при установке (GET): %s", e)
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")


@router.post("/install", response_class=FileResponse, include_in_schema=False)
async def install_post(request: Request):
    """
    POST /install — Б24 шлёт сюда токены при установке локального приложения.
    """
    params = dict(request.query_params)
    form   = dict(await request.form())

    logger.info("install POST query_params: %s", params)
    logger.info("install POST form body: %s", {
        k: v for k, v in form.items() if "ID" not in k
    })

    try:
        await _process_install_payload(params, form)
    except Exception as e:
        logger.error("Ошибка при установке (POST): %s", e)

    return FileResponse(STATIC_DIR / "install.html", media_type="text/html")
