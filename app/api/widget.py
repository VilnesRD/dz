"""
Эндпоинты для виджета Битрикс24:
    GET/POST /bitrix/widget     — HTML виджета (iframe в карточке сделки)
    GET/POST /bitrix/lead-userfield — handler USERFIELD_TYPE для поля лида
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
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


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


def _iter_placements(raw_result):
    """
    Нормализовать result метода placement.list к парам (placement, handler|None).
    Порталы Б24 могут возвращать list[dict], list[str] или dict.
    """
    if isinstance(raw_result, list):
        for item in raw_result:
            if isinstance(item, dict):
                placement = item.get("placement") or item.get("PLACEMENT") or item.get("id")
                handler = item.get("handler") or item.get("HANDLER")
                if placement:
                    yield str(placement), (str(handler) if handler else None)
            elif isinstance(item, str):
                yield item, None
        return

    if isinstance(raw_result, dict):
        if raw_result.get("placement") or raw_result.get("PLACEMENT"):
            placement = raw_result.get("placement") or raw_result.get("PLACEMENT")
            handler = raw_result.get("handler") or raw_result.get("HANDLER")
            yield str(placement), (str(handler) if handler else None)
            return

        for placement, value in raw_result.items():
            if isinstance(value, list):
                if not value:
                    yield str(placement), None
                for entry in value:
                    if isinstance(entry, dict):
                        handler = entry.get("handler") or entry.get("HANDLER") or entry.get("value")
                        yield str(placement), (str(handler) if handler else None)
                    elif isinstance(entry, str):
                        yield str(placement), entry
                    else:
                        yield str(placement), None
            elif isinstance(value, dict):
                handler = value.get("handler") or value.get("HANDLER") or value.get("value")
                yield str(placement), (str(handler) if handler else None)
            elif isinstance(value, str):
                yield str(placement), value
            else:
                yield str(placement), None


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

        target_placements = {"CRM_DEAL_DETAIL_TOOLBAR"}
        for placement_name, handler in _iter_placements(placements):
            if placement_name not in target_placements:
                continue

            payload = {"PLACEMENT": placement_name}
            if handler:
                payload["HANDLER"] = handler
            unbind_resp = await client.post(
                f"https://{domain}/rest/placement.unbind.json",
                params={"auth": auth_id},
                json=payload,
            )
            try:
                unbind_data = unbind_resp.json()
            except Exception:
                unbind_data = {"status_code": unbind_resp.status_code}
            logger.info("placement.unbind %s (%s): %s", placement_name, handler or "no-handler", unbind_data)

        r = await client.post(
            f"https://{domain}/rest/placement.bind.json",
            params={"auth": auth_id},
            json={
                "PLACEMENT": "CRM_DEAL_DETAIL_TOOLBAR",
                "HANDLER": widget_url,
                "TITLE": "Создать документ в Doczilla",
                "DESCRIPTION": "Генерация PDF через Doczilla PRO",
            }
        )
        try:
            data = r.json()
        except Exception:
            data = {"status_code": r.status_code, "text": r.text[:500]}
        logger.info("placement.bind CRM_DEAL_DETAIL_TOOLBAR: %s", data)


def _is_runtime_placement(form: dict, params: dict) -> bool:
    """
    Определить, что /install открыт не для установки, а как рабочий placement.
    """
    placement = _pick(
        form.get("PLACEMENT"),
        params.get("PLACEMENT"),
    )
    status = _pick(form.get("status"), params.get("status"))
    event = _pick(form.get("event"), params.get("event"))

    if event and event.upper() == "ONAPPINSTALL":
        return False
    if status and status.upper() == "L":
        return True
    if placement and placement.upper() != "DEFAULT":
        return True
    return False


def _build_widget_redirect_target(request: Request, params: dict, form: dict) -> str:
    deal_id = _pick(
        params.get("DEAL_ID"),
        form.get("DEAL_ID"),
        form.get("deal_id"),
        form.get("ID"),
        form.get("ENTITY_ID"),
        form.get("ENTITY_VALUE_ID"),
    )
    domain = _normalize_domain(_pick(
        params.get("DOMAIN"),
        params.get("domain"),
        form.get("DOMAIN"),
        form.get("domain"),
        form.get("auth[domain]"),
        params.get("auth[domain]"),
    ))
    placement_options = _pick(
        form.get("PLACEMENT_OPTIONS"),
        params.get("PLACEMENT_OPTIONS"),
        form.get("placement_options"),
        params.get("placement_options"),
    )

    qs = {}
    if deal_id:
        qs["DEAL_ID"] = deal_id
    if domain:
        qs["DOMAIN"] = domain
    if placement_options:
        qs["PLACEMENT_OPTIONS"] = placement_options

    # В reverse-proxy окружении request.url_for() может собрать http-схему.
    # Для Bitrix placement всегда используем публичный HTTPS URL приложения.
    target = f"{settings.APP_PUBLIC_URL.rstrip('/')}/bitrix/widget"
    if qs:
        target = f"{target}?{urlencode(qs)}"
    return target


@router.get("/bitrix/widget", response_class=FileResponse, include_in_schema=False)
async def serve_widget_get(request: Request):
    """GET — Б24 открывает iframe."""
    return FileResponse(STATIC_DIR / "widget.html", media_type="text/html", headers=NO_CACHE_HEADERS)


@router.get("/bitrix/lead-userfield", response_class=FileResponse, include_in_schema=False)
async def serve_lead_userfield_get(request: Request):
    """GET — обработчик пользовательского типа поля (USERFIELD_TYPE) для лида."""
    return FileResponse(STATIC_DIR / "lead_userfield.html", media_type="text/html", headers=NO_CACHE_HEADERS)


@router.get("/assets/doczilla-logo.png", include_in_schema=False)
async def serve_doczilla_logo_png():
    return FileResponse(STATIC_DIR / "doczilla-pink-32x32.png", media_type="image/png", headers=NO_CACHE_HEADERS)


@router.get("/assets/doczilla-logo.svg", include_in_schema=False)
async def serve_doczilla_logo_svg_alias():
    # Backward compatibility for older frontend that still requests .svg
    return FileResponse(STATIC_DIR / "doczilla-pink-32x32.png", media_type="image/png", headers=NO_CACHE_HEADERS)


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


@router.post("/bitrix/lead-userfield", response_class=FileResponse, include_in_schema=False)
async def serve_lead_userfield_post(request: Request):
    """
    POST — Б24 может открыть handler USERFIELD_TYPE через form-data.
    Преобразуем в redirect на GET + query params.
    """
    form = dict(await request.form())
    logger.info("lead-userfield POST form keys: %s", sorted(form.keys()))

    lead_id = _pick(
        form.get("LEAD_ID"),
        form.get("lead_id"),
        form.get("ID"),
        form.get("ENTITY_ID"),
        form.get("ENTITY_VALUE_ID"),
    )
    domain = _normalize_domain(_pick(
        form.get("DOMAIN"),
        form.get("domain"),
        form.get("auth[domain]"),
    ))
    value = _pick(form.get("VALUE"), form.get("value"))
    mode = _pick(form.get("MODE"), form.get("mode"))

    qs = {}
    if lead_id:
        qs["LEAD_ID"] = lead_id
    if domain:
        qs["DOMAIN"] = domain
    if value:
        qs["VALUE"] = value
    if mode:
        qs["MODE"] = mode

    target = str(request.url_for("serve_lead_userfield_get"))
    if qs:
        target = f"{target}?{urlencode(qs)}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/api/widget-config")
async def widget_config():
    """Список активных шаблонов для выпадающего списка в виджете."""
    with SessionLocal() as db:
        templates = repo.list_templates(db)
    active = [
        {
            "key": t.key,
            "name": t.name,
            "result_mode": (getattr(t, "bitrix_result_mode", "both") or "both"),
            "save_link_field": (getattr(t, "bitrix_deal_link_field", "") or ""),
            "save_pdf_field": (getattr(t, "bitrix_deal_pdf_field", "") or ""),
        }
        for t in templates if t.active
    ]
    logger.info("widget-config: active templates=%d", len(active))
    return {"templates": active}


@router.get("/install", response_class=FileResponse, include_in_schema=False)
async def install_get(request: Request):
    """
    GET /install — страница установки.
    Некоторые порталы Б24 передают auth-параметры именно в query-string на GET.
    """
    params = dict(request.query_params)
    if _is_runtime_placement({}, params):
        target = _build_widget_redirect_target(request, params, {})
        logger.info("install GET opened as placement runtime, redirect -> %s", target)
        return RedirectResponse(url=target, status_code=303)

    if params:
        logger.info("install GET query_params: %s", params)
        try:
            await _process_install_payload(params, {})
        except Exception as e:
            logger.error("Ошибка при установке (GET): %s", e)
    return FileResponse(STATIC_DIR / "install.html", media_type="text/html", headers=NO_CACHE_HEADERS)


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

    if _is_runtime_placement(form, params):
        target = _build_widget_redirect_target(request, params, form)
        logger.info("install POST opened as placement runtime, redirect -> %s", target)
        return RedirectResponse(url=target, status_code=303)

    try:
        await _process_install_payload(params, form)
    except Exception as e:
        logger.error("Ошибка при установке (POST): %s", e)

    return FileResponse(STATIC_DIR / "install.html", media_type="text/html", headers=NO_CACHE_HEADERS)
