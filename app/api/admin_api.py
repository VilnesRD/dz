"""
REST API для админ-панели.

Эндпоинты:
  POST /admin/auth/login          — получить JWT-токен
  GET  /admin/auth/me             — проверить токен

  GET    /admin/templates         — список шаблонов
  POST   /admin/templates         — создать шаблон
  GET    /admin/doczilla/published-templates — список опубликованных dotx из Doczilla
  POST   /admin/templates/import-doczilla    — импорт dotx шаблонов из Doczilla
  GET    /admin/templates/{id}    — шаблон + его структура
  PUT    /admin/templates/{id}    — обновить шаблон
  DELETE /admin/templates/{id}    — удалить шаблон
  POST   /admin/templates/{id}/refresh-structure — перезапросить structureRead
  GET    /admin/templates/{id}/mappings          — маппинги шаблона
  PUT    /admin/templates/{id}/mappings          — сохранить маппинги (bulk)

  GET    /admin/logs              — лог генераций

  GET    /admin/bitrix/fields     — список полей Б24 (для подсказок в UI)
"""
from __future__ import annotations
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db import repository as repo
from app.db.models import Template
from app.core.config import get_settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])
settings = get_settings()
DOCZILLA_ROOT_FOLDER_ID = "00000000-0000-0000-0000-000000000000"

# ── JWT / Auth ────────────────────────────────────────────────────────────────
SECRET_KEY  = os.environ.get("ADMIN_SECRET_KEY", "change-me-in-production-please")
ALGORITHM   = "HS256"
TOKEN_EXPIRE_HOURS = 24

pwd_ctx   = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2    = OAuth2PasswordBearer(tokenUrl="/admin/auth/login")


def _make_token(username: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode({"sub": username, "exp": exp}, SECRET_KEY, algorithm=ALGORITHM)


async def current_user(token: Annotated[str, Depends(oauth2)],
                       db: Session = Depends(get_db)):
    exc = HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный токен",
                        headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise exc
    except JWTError:
        raise exc
    user = repo.get_user(db, username)
    if not user:
        raise exc
    return user


# ── Auth ──────────────────────────────────────────────────────────────────────

class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/auth/login", response_model=TokenOut)
async def login(form: OAuth2PasswordRequestForm = Depends(),
                db: Session = Depends(get_db)):
    user = repo.get_user(db, form.username)
    if not user or not pwd_ctx.verify(form.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный логин или пароль")
    return TokenOut(access_token=_make_token(user.username))


@router.get("/auth/me")
async def me(user=Depends(current_user)):
    return {"username": user.username}


# ── Templates ─────────────────────────────────────────────────────────────────

class TemplateIn(BaseModel):
    key: str
    name: str
    doczilla_file_id: str
    doczilla_link: str
    doczilla_folder_id: str = "00000000-0000-0000-0000-000000000000"
    doc_name_template: str = "Документ {deal_id}"
    bitrix_result_mode: Literal["link", "pdf", "both"] = "both"
    bitrix_deal_link_field: str = settings.BITRIX_DEAL_LINK_FIELD
    bitrix_deal_link_multiple: bool = False
    bitrix_deal_pdf_field: str = ""
    bitrix_deal_pdf_multiple: bool = False
    active: bool = True


class DoczillaImportBody(BaseModel):
    section_id: str | None = None
    doczilla_folder_id: str | None = None
    active: bool = True
    # Если True — сразу делаем structureRead для всех импортируемых шаблонов.
    # По умолчанию выключено, чтобы не ловить таймауты reverse-proxy.
    sync_structure: bool = False


@router.get("/templates")
async def list_templates(db: Session = Depends(get_db),
                         _=Depends(current_user)):
    templates = repo.list_templates(db)
    return [_template_to_dict(t) for t in templates]


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn,
                          db: Session = Depends(get_db),
                          _=Depends(current_user)):
    from app.services.doczilla_client import DoczillaClient

    if repo.get_template_by_key(db, body.key):
        raise HTTPException(400, f"Шаблон с ключом '{body.key}' уже существует")
    t = repo.create_template(db, **body.model_dump())
    client = DoczillaClient()
    try:
        await _sync_template_structure_and_mappings(db, client, t.id, t.doczilla_file_id)
    except Exception as e:
        repo.delete_template(db, t.id)
        raise HTTPException(502, f"Не удалось получить структуру шаблона из Doczilla: {e}")
    finally:
        await client.close()
    t = _get_or_404(db, t.id)
    return _template_to_dict(t)


@router.get("/doczilla/published-templates")
async def list_published_doczilla_templates(
    section_id: str | None = None,
    db: Session = Depends(get_db),
    _=Depends(current_user),
):
    """
    Получить список опубликованных шаблонов dotx из раздела Doczilla.
    """
    from app.services.doczilla_client import DoczillaClient

    section = (section_id or settings.DOCZILLA_TEMPLATES_SECTION_ID or "").strip()
    if not section:
        raise HTTPException(400, "Не указан section_id (передайте query или DOCZILLA_TEMPLATES_SECTION_ID в .env)")
    _validate_doczilla_section_id(section)
    log.info("doczilla published-templates: section_id=%s", section)

    client = DoczillaClient()
    try:
        items = await client.get_templates(section)
    finally:
        await client.close()

    result = []
    for item in items:
        record_id = str(item.get("recordId") or "").strip()
        link = str(item.get("link") or "").strip()
        name = str(item.get("name") or "").strip()
        if not (record_id and link and name):
            continue
        exists = db.query(Template).filter(Template.doczilla_file_id == record_id).first()
        result.append({
            "name": name,
            "record_id": record_id,
            "link": link,
            "folder_id": str(item.get("folderId") or "00000000-0000-0000-0000-000000000000"),
            "folder_name": item.get("folderName"),
            "exists": bool(exists),
            "existing_template_id": exists.id if exists else None,
            "suggested_key": _unique_key(db, _slugify(name)),
        })
    return {"section_id": section, "templates": result, "count": len(result)}


@router.post("/templates/import-doczilla")
async def import_doczilla_templates(
    body: DoczillaImportBody,
    db: Session = Depends(get_db),
    _=Depends(current_user),
):
    """
    Импортировать опубликованные шаблоны Doczilla в локальную таблицу templates.
    """
    from app.services.doczilla_client import DoczillaClient

    section = (body.section_id or settings.DOCZILLA_TEMPLATES_SECTION_ID or "").strip()
    if not section:
        raise HTTPException(400, "Не указан section_id (body.section_id или DOCZILLA_TEMPLATES_SECTION_ID)")
    _validate_doczilla_section_id(section)
    log.info(
        "doczilla import: section_id=%s folder_override=%s sync_structure=%s",
        section,
        body.doczilla_folder_id,
        body.sync_structure,
    )

    created = 0
    updated = 0
    skipped = 0
    structure_synced = 0
    structure_failed = 0
    structure_errors: list[str] = []

    client = DoczillaClient()
    try:
        items = await client.get_templates(section)

        for item in items:
            record_id = str(item.get("recordId") or "").strip()
            link = str(item.get("link") or "").strip()
            name = str(item.get("name") or "").strip()
            if not (record_id and link and name):
                skipped += 1
                continue

            existing = db.query(Template).filter(Template.doczilla_file_id == record_id).first()
            folder_id = (
                body.doczilla_folder_id
                or str(item.get("folderId") or "")
                or "00000000-0000-0000-0000-000000000000"
            )
            if existing:
                updated_tpl = repo.update_template(
                    db, existing.id,
                    name=name,
                    doczilla_link=link,
                    doczilla_folder_id=folder_id,
                    active=body.active,
                )
                if updated_tpl and body.sync_structure:
                    try:
                        await _sync_template_structure_and_mappings(
                            db, client, updated_tpl.id, updated_tpl.doczilla_file_id
                        )
                        structure_synced += 1
                    except Exception as e:
                        structure_failed += 1
                        log.warning("structure sync failed template_id=%s file_id=%s: %s", updated_tpl.id, record_id, e)
                        if len(structure_errors) < 5:
                            structure_errors.append(f"{name}: {e}")
                updated += 1
                continue

            key = _unique_key(db, _slugify(name))
            created_tpl = repo.create_template(
                db,
                key=key,
                name=name,
                doczilla_file_id=record_id,
                doczilla_link=link,
                doczilla_folder_id=folder_id,
                doc_name_template="Документ {deal_id} от {date}",
                bitrix_result_mode="both",
                bitrix_deal_link_field=settings.BITRIX_DEAL_LINK_FIELD,
                bitrix_deal_link_multiple=False,
                bitrix_deal_pdf_field="",
                bitrix_deal_pdf_multiple=False,
                active=body.active,
            )
            if created_tpl and body.sync_structure:
                try:
                    await _sync_template_structure_and_mappings(
                        db, client, created_tpl.id, created_tpl.doczilla_file_id
                    )
                    structure_synced += 1
                except Exception as e:
                    structure_failed += 1
                    log.warning("structure sync failed template_id=%s file_id=%s: %s", created_tpl.id, record_id, e)
                    if len(structure_errors) < 5:
                        structure_errors.append(f"{name}: {e}")
            created += 1
    finally:
        await client.close()

    return {
        "section_id": section,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": len(items),
        "structure_synced": structure_synced,
        "structure_failed": structure_failed,
        "structure_errors": structure_errors,
        "structure_sync_enabled": body.sync_structure,
    }


@router.get("/templates/{tid}")
async def get_template(tid: int, db: Session = Depends(get_db),
                       _=Depends(current_user)):
    t = _get_or_404(db, tid)
    d = _template_to_dict(t)
    # Включаем распарсенную структуру
    if t.structure_json:
        d["structure"] = json.loads(t.structure_json)
    return d


@router.put("/templates/{tid}")
async def update_template(tid: int, body: TemplateIn,
                          db: Session = Depends(get_db),
                          _=Depends(current_user)):
    from app.services.doczilla_client import DoczillaClient

    t = _get_or_404(db, tid)
    # Если file_id изменился — сбросить кэш структуры
    file_id_changed = t.doczilla_file_id != body.doczilla_file_id
    if file_id_changed:
        repo.update_template(db, tid, structure_json=None, structure_updated_at=None)
    t = repo.update_template(db, tid, **body.model_dump())
    if t and (file_id_changed or not t.structure_json):
        client = DoczillaClient()
        try:
            await _sync_template_structure_and_mappings(db, client, t.id, t.doczilla_file_id)
        except Exception as e:
            raise HTTPException(502, f"Не удалось обновить структуру шаблона из Doczilla: {e}")
        finally:
            await client.close()
        t = _get_or_404(db, tid)
    return _template_to_dict(t)


@router.delete("/templates/{tid}", status_code=204)
async def delete_template(tid: int, db: Session = Depends(get_db),
                          _=Depends(current_user)):
    if not repo.delete_template(db, tid):
        raise HTTPException(404, "Шаблон не найден")


@router.post("/templates/{tid}/refresh-structure")
async def refresh_structure(tid: int, db: Session = Depends(get_db),
                            _=Depends(current_user)):
    """
    Перезапросить structureRead из Doczilla для шаблона.
    Возвращает распарсенные переменные для отображения в UI маппера.
    """
    from app.services.doczilla_client import DoczillaClient
    t = _get_or_404(db, tid)

    client = DoczillaClient()
    try:
        variables = await _sync_template_structure_and_mappings(db, client, t.id, t.doczilla_file_id)
    finally:
        await client.close()

    return {"variables": variables, "count": len(variables)}


# ── Mappings ──────────────────────────────────────────────────────────────────

class MappingItem(BaseModel):
    variable_id: str
    source_type: str   # field | formula | literal | selector_map | skip
    source_value: str = ""


@router.get("/templates/{tid}/mappings")
async def get_mappings(tid: int, db: Session = Depends(get_db),
                       _=Depends(current_user)):
    from app.services.doczilla_client import DoczillaClient

    t = _get_or_404(db, tid)
    mappings = repo.get_mappings(db, tid)
    if not mappings or not t.structure_json:
        client = DoczillaClient()
        try:
            await _sync_template_structure_and_mappings(db, client, t.id, t.doczilla_file_id)
            mappings = repo.get_mappings(db, tid)
            log.info("mappings auto-initialized: template_id=%s count=%s", tid, len(mappings))
        except Exception as e:
            raise HTTPException(502, f"Не удалось инициализировать маппинг из структуры Doczilla: {e}")
        finally:
            await client.close()
    return [_mapping_to_dict(m) for m in mappings]


@router.put("/templates/{tid}/mappings")
async def save_mappings(tid: int, body: list[MappingItem],
                        db: Session = Depends(get_db),
                        _=Depends(current_user)):
    """Bulk-сохранение маппингов. Принимает список изменённых строк."""
    _get_or_404(db, tid)
    for item in body:
        repo.upsert_mapping(db, tid,
            variable_id  = item.variable_id,
            source_type  = item.source_type,
            source_value = item.source_value,
        )
    return {"saved": len(body)}


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/logs")
async def get_logs(limit: int = 100, db: Session = Depends(get_db),
                   _=Depends(current_user)):
    logs = repo.list_logs(db, limit)
    return [_log_to_dict(l) for l in logs]


# ── Bitrix fields helper ──────────────────────────────────────────────────────

@router.get("/bitrix/fields")
async def get_bitrix_fields(
    domain: str | None = None,
    auth_id: str | None = None,
    _=Depends(current_user),
):
    """
    Возвращает список доступных полей Б24 для подсказок в маппере.
    Используется как справочник в UI (autocomplete).
    """
    from app.services.bitrix_client import BitrixClient, BitrixError
    from app.db.database import SessionLocal
    from app.db import repository as repo

    fields: list[dict] = []

    if not domain:
        with SessionLocal() as db:
            token = repo.get_latest_oauth_token(db)
        domain = token.domain if token else None

    if not domain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Не найден домен Битрикс24. Переустановите локальное приложение и повторите запрос.",
        )

    client = BitrixClient(domain=domain, access_token=auth_id)
    entity_defs = {
        "deal": {
            "legacy_method": "crm.deal.fields",
            "item_entity_type_id": 2,
            "uf_method": "crm.deal.userfield.list",
            "label_prefix": "Сделка",
        },
        "lead": {
            "legacy_method": "crm.lead.fields",
            "item_entity_type_id": 1,
            "uf_method": "crm.lead.userfield.list",
            "label_prefix": "Лид",
        },
        "contact": {
            "legacy_method": "crm.contact.fields",
            "item_entity_type_id": 3,
            "uf_method": "crm.contact.userfield.list",
            "label_prefix": "Контакт",
        },
        "company": {
            "legacy_method": "crm.company.fields",
            "item_entity_type_id": 4,
            "uf_method": "crm.company.userfield.list",
            "label_prefix": "Компания",
        },
    }
    stats = {k: 0 for k in entity_defs.keys()}
    custom_stats = {k: 0 for k in entity_defs.keys()}
    uf_label_stats = {k: 0 for k in entity_defs.keys()}
    errors: list[str] = []
    try:
        # Названия кастомных UF_* полей берём из userfield.list
        uf_meta_by_entity: dict[str, dict[str, dict[str, Any]]] = {k: {} for k in entity_defs.keys()}
        uf_labels_by_entity: dict[str, dict[str, str]] = {k: {} for k in entity_defs.keys()}
        for entity, cfg in entity_defs.items():
            try:
                uf_rows = await _call_bitrix_list_all(client, cfg["uf_method"])
                uf_meta = _build_userfield_meta_map(uf_rows)
                uf_labels = {code: str(meta.get("label") or code) for code, meta in uf_meta.items()}
                uf_meta_by_entity[entity] = uf_meta
                uf_labels_by_entity[entity] = uf_labels
                uf_label_stats[entity] = len(uf_labels)
            except Exception as e:
                log.warning("%s: %s", cfg["uf_method"], e)
                errors.append(f"{entity}.userfield: {e}")

        for entity, cfg in entity_defs.items():
            entity_fields = await _load_crm_entity_fields(
                client=client,
                legacy_method=cfg["legacy_method"],
                item_entity_type_id=cfg["item_entity_type_id"],
                log_key=entity,
                errors=errors,
            )
            uf_meta = uf_meta_by_entity.get(entity, {})
            uf_labels = uf_labels_by_entity.get(entity, {})
            for fname, finfo in entity_fields.items():
                code = str(fname)
                norm_code = _normalize_uf_code(code)
                is_custom = _is_uf_field_code(code)
                row_meta = uf_meta.get(norm_code, {})
                title = _extract_field_title(finfo, code)
                if is_custom:
                    title = _resolve_userfield_title(code, finfo, uf_labels)
                field_type = _extract_field_type(finfo)
                user_type_id = _normalize_user_type_id(row_meta.get("user_type_id")) or _extract_field_user_type_id(finfo)
                is_multiple = bool(row_meta.get("is_multiple")) or _extract_field_is_multiple(finfo)
                can_store_link, can_store_pdf = _detect_result_storage_capabilities(field_type, user_type_id)
                fields.append({
                    "path": f"{entity}.{code}",
                    "entity": entity,
                    "code": code,
                    "label": f"{cfg['label_prefix']}: {title}",
                    "type": field_type,
                    "user_type_id": user_type_id,
                    "is_custom": is_custom,
                    "is_multiple": is_multiple,
                    "can_store_link": can_store_link,
                    "can_store_pdf": can_store_pdf,
                })
            stats[entity] = len(entity_fields)
            custom_stats[entity] = len([k for k in entity_fields.keys() if str(k).upper().startswith("UF_")])
    except BitrixError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    finally:
        await client.close()

    fields.sort(key=lambda x: x.get("path", ""))
    log.info("bitrix fields loaded: stats=%s total=%d | custom=%s | uf labels=%s", stats, len(fields), custom_stats, uf_label_stats)
    return {
        "fields": fields,
        "meta": {
            "stats": stats,
            "custom_stats": custom_stats,
            "uf_label_stats": uf_label_stats,
            "errors": errors,
        },
    }


@router.get("/bitrix/field-options")
async def get_bitrix_field_options(
    path: str,
    domain: str | None = None,
    auth_id: str | None = None,
    _=Depends(current_user),
):
    """
    Вернуть варианты значений для конкретного поля Б24.
    Используется UI-конструктором условий (selector_map).
    """
    from app.services.bitrix_client import BitrixClient, BitrixError
    from app.db.database import SessionLocal
    from app.db import repository as repo

    raw_path = str(path or "").strip()
    if "." not in raw_path:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Параметр path должен быть в формате entity.FIELD")
    entity, field_code = raw_path.split(".", 1)
    entity = str(entity or "").strip().lower()
    field_code = str(field_code or "").strip()
    if entity not in {"deal", "lead", "contact", "company"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Поддерживаются только entity: deal/lead/contact/company")
    if not field_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не указан код поля")

    if not domain:
        with SessionLocal() as db:
            token = repo.get_latest_oauth_token(db)
        domain = token.domain if token else None
    if not domain:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Не найден домен Битрикс24. Переустановите локальное приложение и повторите запрос.",
        )

    entity_cfg = {
        "deal": {
            "legacy_method": "crm.deal.fields",
            "item_entity_type_id": 2,
            "uf_method": "crm.deal.userfield.list",
        },
        "lead": {
            "legacy_method": "crm.lead.fields",
            "item_entity_type_id": 1,
            "uf_method": "crm.lead.userfield.list",
        },
        "contact": {
            "legacy_method": "crm.contact.fields",
            "item_entity_type_id": 3,
            "uf_method": "crm.contact.userfield.list",
        },
        "company": {
            "legacy_method": "crm.company.fields",
            "item_entity_type_id": 4,
            "uf_method": "crm.company.userfield.list",
        },
    }[entity]

    client = BitrixClient(domain=domain, access_token=auth_id)
    errors: list[str] = []
    options: list[dict[str, str]] = []
    field_meta: dict[str, Any] = {}
    try:
        fields = await _load_crm_entity_fields(
            client=client,
            legacy_method=entity_cfg["legacy_method"],
            item_entity_type_id=entity_cfg["item_entity_type_id"],
            log_key=f"{entity}.field_options",
            errors=errors,
        )
        field_meta = fields.get(field_code) or fields.get(field_code.upper()) or {}
        norm_code = _normalize_uf_code(field_code)
        if not field_meta and norm_code in fields:
            field_meta = fields.get(norm_code) or {}
        field_type = _extract_field_type(field_meta)
        user_type_id = _extract_field_user_type_id(field_meta)
        is_multiple = _extract_field_is_multiple(field_meta)
        title = _extract_field_title(field_meta, field_code)

        # 1) Для UF_* сначала пытаемся взять справочник из userfield.list
        if _is_uf_field_code(field_code):
            try:
                uf_rows = await _call_bitrix_list_all(client, entity_cfg["uf_method"])
                uf_meta_map = _build_userfield_meta_map(uf_rows)
                uf_meta = uf_meta_map.get(norm_code, {})
                uf_row = _find_userfield_row_by_code(uf_rows, norm_code)
                options = _extract_userfield_options(uf_row)
                if uf_meta:
                    if not user_type_id:
                        user_type_id = _normalize_user_type_id(uf_meta.get("user_type_id"))
                    if not is_multiple:
                        is_multiple = bool(uf_meta.get("is_multiple"))
                if _is_code_like_title(title, norm_code):
                    title = _resolve_userfield_title(norm_code, field_meta or uf_row, {
                        norm_code: str(uf_meta.get("label") or norm_code),
                    })
            except Exception as e:
                errors.append(f"{entity}.userfield.list: {e}")

        # 2) Варианты, если они пришли прямо в meta поля
        if not options:
            options = _extract_enum_options_from_meta(field_meta)

        # 3) Для crm_status пытаемся получить справочник crm.status.list
        status_entity_id = _guess_status_entity_id(entity, field_code, field_meta)
        if not options and status_entity_id:
            try:
                status_rows = await _call_bitrix_list_all(
                    client,
                    "crm.status.list",
                    {"filter": {"ENTITY_ID": status_entity_id}},
                )
                options = _extract_status_options(status_rows)
            except Exception as e:
                errors.append(f"crm.status.list({status_entity_id}): {e}")

        # 4) Приведение/дедупликация
        options = _normalize_option_rows(options)
        return {
            "path": f"{entity}.{field_code}",
            "field": {
                "entity": entity,
                "code": field_code,
                "label": title,
                "type": field_type,
                "user_type_id": user_type_id,
                "is_multiple": bool(is_multiple),
            },
            "options": options,
            "meta": {
                "count": len(options),
                "status_entity_id": status_entity_id or "",
                "errors": errors,
            },
        }
    except BitrixError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    finally:
        await client.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_404(db: Session, tid: int):
    t = repo.get_template(db, tid)
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    return t


def _template_to_dict(t) -> dict:
    configured_count = sum(1 for m in t.mappings if _is_mapping_configured(m))
    return {
        "id": t.id, "key": t.key, "name": t.name,
        "doczilla_file_id": t.doczilla_file_id,
        "doczilla_link": t.doczilla_link,
        "doczilla_folder_id": t.doczilla_folder_id,
        "doc_name_template": t.doc_name_template,
        "bitrix_result_mode": getattr(t, "bitrix_result_mode", "both") or "both",
        "bitrix_deal_link_field": t.bitrix_deal_link_field or "",
        "bitrix_deal_link_multiple": bool(getattr(t, "bitrix_deal_link_multiple", False)),
        "bitrix_deal_pdf_field": t.bitrix_deal_pdf_field or "",
        "bitrix_deal_pdf_multiple": bool(getattr(t, "bitrix_deal_pdf_multiple", False)),
        "active": t.active,
        "has_structure": bool(t.structure_json),
        "structure_updated_at": t.structure_updated_at.isoformat() if t.structure_updated_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "mappings_count": configured_count,
        "mappings_total": len(t.mappings),
    }


def _mapping_to_dict(m) -> dict:
    return {
        "id": m.id,
        "variable_id": m.variable_id,
        "variable_name": m.variable_name,
        "variable_kind": m.variable_kind,
        "variable_type": m.variable_type,
        "source_type": m.source_type,
        "source_value": m.source_value,
        "parent_variable_id": m.parent_variable_id,
    }


def _log_to_dict(l) -> dict:
    return {
        "id": l.id, "deal_id": l.deal_id,
        "template_key": l.template_key, "doc_name": l.doc_name,
        "doc_link": l.doc_link, "status": l.status,
        "error_message": l.error_message,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    }


def _flatten_structure(scheme: dict) -> list[dict]:
    """
    Развернуть вложенную структуру Doczilla в плоский список переменных.
    Включает вложенные элементы с указанием parent_id.
    """
    result = []

    def _walk(elements, parent_id=None):
        if isinstance(elements, dict):
            items = elements.items()
        elif isinstance(elements, list):
            # replicator: список dict {id: {...}}
            items = []
            for row in elements:
                if isinstance(row, dict):
                    items.extend(row.items())
        else:
            return

        for eid, el in items:
            if not isinstance(el, dict):
                continue
            result.append({
                "id":        el.get("id", eid),
                "name":      el.get("name", ""),
                "kind":      el.get("kind", ""),
                "type":      el.get("type", ""),
                "index":     el.get("index", 0),
                "parent_id": parent_id,
                "selector_type": el.get("selectorType"),
            })
            if "elements" in el:
                _walk(el["elements"], parent_id=el.get("id", eid))

    _walk(scheme)
    result.sort(key=lambda x: (x.get("parent_id") or 0, x.get("index", 0)))
    return result


def _slugify(value: str) -> str:
    """
    Базовый slug для key шаблона (латиница/цифры/дефис).
    """
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text or "template"


def _unique_key(db: Session, base: str) -> str:
    key = base
    i = 2
    while repo.get_template_by_key(db, key):
        key = f"{base}-{i}"
        i += 1
    return key


def _validate_doczilla_section_id(section_id: str) -> None:
    """
    section_id в Doczilla — это ID раздела, не folderId.
    Частая ошибка: передают root folderId (0000...).
    """
    if section_id.strip() == DOCZILLA_ROOT_FOLDER_ID:
        raise HTTPException(
            400,
            "Передан folderId root (0000...), а нужен section_id раздела Doczilla. "
            "Используйте section вроде 039BC112-E801-4F82-BA21-484F72500736.",
        )


def _extract_bitrix_fields(payload: Any) -> dict[str, dict]:
    """
    Нормализовать формат ответа *crm.*.fields к словарю {FIELD_CODE: meta}.
    """
    if not payload:
        return {}
    if isinstance(payload, dict):
        raw = payload.get("fields") if isinstance(payload.get("fields"), dict) else payload
        out: dict[str, dict] = {}
        for code, meta in raw.items():
            if isinstance(meta, dict):
                out[str(code)] = meta
        return out
    return {}


def _extract_localized_label(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        # Б24 может вернуть мультиязычные подписи как массив объектов:
        # [{"LANG":"ru","VALUE":"..."}, {"LANG":"en","VALUE":"..."}]
        preferred: list[str] = []
        fallback: list[str] = []
        for row in value:
            if not isinstance(row, dict):
                label = _extract_localized_label(row)
                if label:
                    fallback.append(label)
                continue
            lang = str(
                row.get("LANG")
                or row.get("lang")
                or row.get("language")
                or ""
            ).strip().lower()
            raw = (
                row.get("VALUE")
                or row.get("value")
                or row.get("TEXT")
                or row.get("text")
                or row.get("LABEL")
                or row.get("label")
                or row.get("TITLE")
                or row.get("title")
                or row.get("NAME")
                or row.get("name")
            )
            label = _extract_localized_label(raw)
            if not label:
                continue
            if lang.startswith("ru"):
                preferred.append(label)
            else:
                fallback.append(label)
        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
        return ""
    if isinstance(value, dict):
        direct = (
            value.get("VALUE")
            or value.get("value")
            or value.get("TEXT")
            or value.get("text")
            or value.get("LABEL")
            or value.get("label")
            or value.get("TITLE")
            or value.get("title")
            or value.get("NAME")
            or value.get("name")
        )
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        for key in ("ru", "ru_RU", "en", "en_US"):
            val = value.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in value.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _pick_row_value(row: dict[str, Any], *keys: str) -> Any:
    """
    Достать значение из dict с учётом разных кейсов/стилей ключей:
    FIELD_NAME / fieldName / field_name.
    """
    if not isinstance(row, dict):
        return None

    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value

    lowered = {str(k).lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(str(key).lower())
        if value not in (None, ""):
            return value
    return None


def _normalize_uf_code(code: Any) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    if re.match(r"(?i)^uf(?:_|)?crm", text) or text.upper().startswith("UF_"):
        return _canonicalize_uf_code(text)
    legacy = _to_legacy_field_code(text)
    if legacy:
        return legacy
    return text.upper()


def _is_uf_field_code(code: Any) -> bool:
    return _normalize_uf_code(code).startswith("UF_")


def _is_code_like_title(title: str, code: str) -> bool:
    t = str(title or "").strip()
    c = _normalize_uf_code(code)
    if not t:
        return True
    t_norm = _normalize_uf_code(t)
    if t_norm == c:
        return True
    return bool(re.fullmatch(r"UF_[A-Z0-9_]+", t_norm))


def _extract_field_title(meta: Any, fallback: str = "") -> str:
    """
    Извлечь человекочитаемое имя поля из разных форматов ответа Б24.
    """
    if not isinstance(meta, dict):
        return str(fallback or "").strip()

    for key in (
        "title",
        "TITLE",
        "name",
        "NAME",
        "label",
        "LABEL",
        "formLabel",
        "FORM_LABEL",
        "listLabel",
        "LIST_LABEL",
        "EDIT_FORM_LABEL",
        "editFormLabel",
        "LIST_COLUMN_LABEL",
        "listColumnLabel",
        "LIST_FILTER_LABEL",
        "listFilterLabel",
        "caption",
        "CAPTION",
    ):
        value = _pick_row_value(meta, key)
        label = _extract_localized_label(value)
        if label:
            return label

    return str(fallback or "").strip()


def _resolve_userfield_title(code: Any, meta: Any, uf_labels: dict[str, str]) -> str:
    """
    Найти лучшее название кастомного UF-поля:
    1) из crm.*.userfield.list
    2) из метаданных crm.*.fields / crm.item.fields
    """
    norm_code = _normalize_uf_code(code)
    map_label = (
        uf_labels.get(norm_code)
        or uf_labels.get(str(code))
        or uf_labels.get(str(code).upper())
        or ""
    )
    meta_label = _extract_field_title(meta, norm_code)

    if map_label and not _is_code_like_title(map_label, norm_code):
        return map_label
    if meta_label and not _is_code_like_title(meta_label, norm_code):
        return meta_label
    if map_label:
        return map_label
    if meta_label:
        return meta_label
    return _humanize_uf_fallback_title(norm_code)


def _humanize_uf_fallback_title(code: str) -> str:
    """
    Человекочитаемый fallback для UF-полей, если Б24 не вернул label.
    """
    norm = _normalize_uf_code(code)
    if not norm:
        return "Пользовательское поле"
    if not norm.startswith("UF_"):
        return norm

    tail = norm[3:]
    if tail.startswith("CRM_"):
        tail = tail[4:]
    tail = tail.strip("_")
    if not tail:
        return "Пользовательское поле"

    return f"Пользовательское поле {tail}"


def _normalize_user_type_id(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _extract_field_type(meta: Any) -> str:
    if not isinstance(meta, dict):
        return ""
    value = _pick_row_value(meta, "type", "TYPE", "fieldType", "FIELD_TYPE")
    if isinstance(value, dict):
        value = _pick_row_value(value, "id", "ID", "type", "TYPE", "name", "NAME", "value", "VALUE")
    return str(value or "").strip().lower()


def _extract_field_user_type_id(meta: Any) -> str:
    if not isinstance(meta, dict):
        return ""
    value = _pick_row_value(meta, "userType", "USER_TYPE_ID", "userTypeId", "USER_TYPE")
    if isinstance(value, dict):
        value = _pick_row_value(
            value,
            "id", "ID",
            "type", "TYPE",
            "userTypeId", "USER_TYPE_ID",
            "name", "NAME",
            "value", "VALUE",
        )
    return _normalize_user_type_id(value)


def _to_bool_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().upper()
    return text in {"Y", "YES", "TRUE", "1"}


def _extract_field_is_multiple(meta: Any) -> bool:
    if not isinstance(meta, dict):
        return False
    value = _pick_row_value(meta, "isMultiple", "IS_MULTIPLE", "multiple", "MULTIPLE")
    return _to_bool_flag(value)


def _detect_result_storage_capabilities(field_type: str, user_type_id: str) -> tuple[bool, bool]:
    ft = str(field_type or "").strip().lower()
    ut = _normalize_user_type_id(user_type_id)

    can_store_pdf = ft in {"file"} or ut in {"file", "disk_file", "document"}
    can_store_link = ft in {"url"} or ut in {"url", "webaddress", "hyperlink"}

    return can_store_link, can_store_pdf


def _normalize_option_rows(items: Any) -> list[dict[str, str]]:
    """
    Привести массив вариантов к формату:
      [{"value":"...", "label":"..."}, ...]
    """
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in items:
        value = ""
        label = ""
        if isinstance(row, dict):
            value = str(
                row.get("value")
                or row.get("VALUE")
                or row.get("id")
                or row.get("ID")
                or row.get("statusId")
                or row.get("STATUS_ID")
                or ""
            ).strip()
            label = _extract_localized_label(
                row.get("label")
                or row.get("LABEL")
                or row.get("name")
                or row.get("NAME")
                or row.get("title")
                or row.get("TITLE")
                or row.get("VALUE")
            )
        elif row is not None:
            value = str(row).strip()
            label = value
        if not value:
            continue
        if not label:
            label = value
        key = f"{value}|{label}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"value": value, "label": label})
    return out


def _extract_enum_options_from_meta(meta: Any) -> list[dict[str, str]]:
    """
    Достать справочник значений из метаданных поля (items/enum/values/list).
    """
    if not isinstance(meta, dict):
        return []
    for key in (
        "items", "ITEMS",
        "enum", "ENUM",
        "values", "VALUES",
        "list", "LIST",
        "options", "OPTIONS",
    ):
        value = _pick_row_value(meta, key)
        if isinstance(value, list):
            normalized = _normalize_option_rows(value)
            if normalized:
                return normalized
    return []


def _extract_userfield_options(row: Any) -> list[dict[str, str]]:
    """
    Варианты значений UF-поля (enumeration), если они пришли в userfield.list.
    """
    if not isinstance(row, dict):
        return []
    value = _pick_row_value(row, "LIST", "list", "items", "ITEMS", "ENUM", "enum")
    if isinstance(value, list):
        return _normalize_option_rows(value)
    return []


def _find_userfield_row_by_code(rows: Any, code: str) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {}
    norm = _normalize_uf_code(code)
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_code = _normalize_uf_code(_pick_row_value(row, "FIELD_NAME", "fieldName", "field_name"))
        if row_code and row_code == norm:
            return row
    return {}


def _extract_status_options(rows: Any) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(
            row.get("STATUS_ID")
            or row.get("statusId")
            or row.get("ID")
            or row.get("id")
            or ""
        ).strip()
        label = _extract_localized_label(
            row.get("NAME")
            or row.get("name")
            or row.get("TITLE")
            or row.get("title")
            or value
        )
        if value and label:
            out.append({"value": value, "label": label})
    return _normalize_option_rows(out)


def _guess_status_entity_id(entity: str, field_code: str, meta: Any) -> str:
    """
    Попытаться определить ENTITY_ID для crm.status.list.
    """
    if not isinstance(meta, dict):
        meta = {}
    code = str(field_code or "").strip().upper()
    status_id = str(
        _pick_row_value(
            meta,
            "statusType",
            "STATUS_TYPE",
            "statusTypeId",
            "STATUS_TYPE_ID",
            "statusEntityId",
            "STATUS_ENTITY_ID",
            "entityId",
            "ENTITY_ID",
        ) or ""
    ).strip().upper()
    if status_id:
        return status_id

    fallback_map = {
        "deal": {
            "STAGE_ID": "DEAL_STAGE",
            "TYPE_ID": "DEAL_TYPE",
            "SOURCE_ID": "SOURCE",
            "EVENT_ID": "EVENT_TYPE",
        },
        "lead": {
            "STATUS_ID": "STATUS",
            "SOURCE_ID": "SOURCE",
            "HONORIFIC": "HONORIFIC",
        },
        "contact": {
            "TYPE_ID": "CONTACT_TYPE",
            "SOURCE_ID": "SOURCE",
            "HONORIFIC": "HONORIFIC",
        },
        "company": {
            "TYPE_ID": "COMPANY_TYPE",
            "SOURCE_ID": "SOURCE",
            "INDUSTRY": "INDUSTRY",
            "EMPLOYEES": "EMPLOYEES",
            "HONORIFIC": "HONORIFIC",
        },
    }
    return str(fallback_map.get(str(entity or "").lower(), {}).get(code, "") or "").strip().upper()


async def _call_bitrix_list_all(client, method: str, params: dict | None = None) -> list[dict]:
    """
    Собрать все страницы list-метода Б24 (result + next).
    """
    rows: list[dict] = []
    start = 0
    base = dict(params or {})

    while True:
        payload = dict(base)
        payload["start"] = start
        response = await client.call_raw(method, payload)
        result = response.get("result")
        chunk: list[Any]
        if isinstance(result, list):
            chunk = result
        elif isinstance(result, dict):
            items = result.get("items")
            chunk = items if isinstance(items, list) else []
        else:
            chunk = []
        rows.extend(item for item in chunk if isinstance(item, dict))

        next_value = response.get("next")
        if next_value is None:
            break
        try:
            start = int(next_value)
        except (TypeError, ValueError):
            break

    return rows


async def _load_crm_entity_fields(
    *,
    client,
    legacy_method: str,
    item_entity_type_id: int,
    log_key: str,
    errors: list[str],
) -> dict[str, dict]:
    """
    Загрузить поля CRM-сущности и объединить 2 источника:
    1) legacy *crm.*.fields (коды совпадают с crm.*.get, например TITLE/UF_*)
    2) crm.item.fields(entityTypeId=...) (более полный справочник полей)
    """
    legacy_fields: dict[str, dict] = {}
    item_fields_raw: dict[str, dict] = {}
    item_fields_legacy: dict[str, dict] = {}

    try:
        legacy_fields = _extract_bitrix_fields(await client.call(legacy_method))
    except Exception as e:
        log.warning("%s: %s", legacy_method, e)
        errors.append(f"{log_key}.legacy_fields: {e}")

    try:
        item_payload = await client.call("crm.item.fields", {"entityTypeId": item_entity_type_id})
        item_fields_raw = _extract_bitrix_fields(item_payload)
    except Exception as e:
        log.warning("crm.item.fields(%s): %s", item_entity_type_id, e)
        errors.append(f"{log_key}.item_fields: {e}")

    for code, meta in item_fields_raw.items():
        legacy_code = _to_legacy_field_code(code)
        if not legacy_code:
            continue
        # Для UF_* используем только реальные коды из legacy crm.*.fields.
        # Это защищает от артефактов конвертации camelCase из crm.item.fields,
        # которые иногда дают несуществующие варианты кода.
        if legacy_code.startswith("UF_"):
            legacy_code = _match_legacy_uf_code(legacy_code, legacy_fields)
            if not legacy_code:
                continue
        if not isinstance(meta, dict):
            meta = {}
        existing = item_fields_legacy.get(legacy_code)
        # Предпочитаем запись, где есть заголовок
        if (
            not existing
            or (
                _is_code_like_title(_extract_field_title(existing, legacy_code), legacy_code)
                and not _is_code_like_title(_extract_field_title(meta, legacy_code), legacy_code)
            )
        ):
            item_fields_legacy[legacy_code] = dict(meta)

    merged = {str(k): dict(v) for k, v in legacy_fields.items() if isinstance(v, dict)}
    for code, meta in item_fields_legacy.items():
        if code not in merged:
            merged[code] = dict(meta)
            continue
        _enrich_field_meta(merged[code], meta, code)

    log.info(
        "crm fields merged: %s legacy=%d item=%d merged=%d",
        log_key, len(legacy_fields), len(item_fields_legacy), len(merged),
    )
    return merged


def _to_legacy_field_code(code: Any) -> str:
    """
    Привести код поля из crm.item.fields (camelCase) к формату legacy (UPPER_SNAKE),
    чтобы пути были совместимы с crm.deal.get / crm.contact.get / crm.company.get.
    """
    text = str(code or "").strip()
    if not text:
        return ""

    if re.match(r"(?i)^uf(?:_|)?crm", text) or text.upper().startswith("UF_"):
        return _canonicalize_uf_code(text)

    if text.upper() == text:
        text = text.replace("-", "_")
        text = re.sub(r"_+", "_", text).strip("_")
        return text.upper()

    # camelCase -> snake_case
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    # letter/number boundaries: parentId2 -> parent_id_2
    snake = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", snake)
    snake = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", snake)
    snake = snake.replace("-", "_")
    snake = re.sub(r"_+", "_", snake).strip("_")
    return snake.upper()


def _canonicalize_uf_code(code: Any) -> str:
    """
    Нормализовать любой вариант UF-кода к каноничному UPPER_SNAKE.

    Примеры:
      ufCrm1752072396863  -> UF_CRM_1752072396863
      UF_CRM_abcDef       -> UF_CRM_ABCDEF
      uf_crm_chat         -> UF_CRM_CHAT
    """
    text = str(code or "").strip()
    if not text:
        return ""
    text = text.replace("-", "_")

    m = re.match(r"(?i)^uf(?:_|)?crm(?:_|)?(.*)$", text)
    if m:
        tail = re.sub(r"[^A-Za-z0-9_]", "", m.group(1) or "")
        tail = re.sub(r"_+", "_", tail).strip("_")
        return f"UF_CRM_{tail.upper()}" if tail else "UF_CRM"

    if text.upper().startswith("UF_"):
        tail = re.sub(r"[^A-Za-z0-9_]", "", text[3:])
        tail = re.sub(r"_+", "_", tail).strip("_")
        return f"UF_{tail.upper()}" if tail else "UF"

    return text.upper()


def _compact_code(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _match_legacy_uf_code(code: str, legacy_fields: dict[str, dict]) -> str:
    """
    Сопоставить UF-код из crm.item.fields с реальным кодом из legacy crm.*.fields.
    Возвращает пустую строку, если соответствие не найдено.
    """
    norm = _canonicalize_uf_code(code)
    if not norm.startswith("UF_"):
        return norm
    if norm in legacy_fields:
        return norm

    compact = _compact_code(norm)
    if not compact:
        return ""

    for legacy_code in legacy_fields.keys():
        lc = str(legacy_code)
        if not lc.upper().startswith("UF_"):
            continue
        if _compact_code(lc) == compact:
            return lc
    return ""


def _enrich_field_meta(base: dict[str, Any], extra: dict[str, Any], field_code: str) -> None:
    """
    Аккуратно обогатить legacy-метаданные полей данными из crm.item.fields:
    - копируем отсутствующие ключи;
    - переносим title, если в legacy он кодоподобный, а в extra человекочитаемый;
    - добираем type/userType/isMultiple при их отсутствии в legacy.
    """
    if not isinstance(base, dict) or not isinstance(extra, dict):
        return

    for key, value in extra.items():
        if key not in base or base.get(key) in (None, "", [], {}):
            base[key] = value

    base_title = _extract_field_title(base, field_code)
    extra_title = _extract_field_title(extra, field_code)
    if (
        extra_title
        and not _is_code_like_title(extra_title, field_code)
        and (not base_title or _is_code_like_title(base_title, field_code))
    ):
        base["title"] = extra_title

    if not _extract_field_type(base):
        extra_type = _extract_field_type(extra)
        if extra_type:
            base["type"] = extra_type

    if not _extract_field_user_type_id(base):
        extra_ut = _extract_field_user_type_id(extra)
        if extra_ut:
            base["userType"] = extra_ut

    if not _extract_field_is_multiple(base) and _extract_field_is_multiple(extra):
        base["isMultiple"] = True


def _build_userfield_meta_map(payload: Any) -> dict[str, dict[str, Any]]:
    """
    Преобразовать ответ crm.*.userfield.list в map FIELD_NAME -> meta.
    """
    if not isinstance(payload, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = str(_pick_row_value(row, "FIELD_NAME", "fieldName", "field_name") or "").strip()
        if not code:
            continue
        norm_code = _normalize_uf_code(code)
        user_type_id = _normalize_user_type_id(_pick_row_value(row, "USER_TYPE_ID", "userTypeId", "userType"))
        is_multiple = _to_bool_flag(_pick_row_value(row, "MULTIPLE", "multiple", "isMultiple", "IS_MULTIPLE"))
        label = (
            _extract_localized_label(_pick_row_value(row, "EDIT_FORM_LABEL", "editFormLabel"))
            or _extract_localized_label(_pick_row_value(row, "LIST_COLUMN_LABEL", "listColumnLabel"))
            or _extract_localized_label(_pick_row_value(row, "LIST_FILTER_LABEL", "listFilterLabel"))
            or _extract_localized_label(_pick_row_value(row, "LABEL", "label", "TITLE", "title", "NAME", "name"))
            or norm_code
        )
        out[norm_code] = {
            "code": norm_code,
            "label": label,
            "user_type_id": user_type_id,
            "is_multiple": is_multiple,
        }
    return out


def _build_userfield_label_map(payload: Any) -> dict[str, str]:
    meta = _build_userfield_meta_map(payload)
    return {code: str(item.get("label") or code) for code, item in meta.items()}


def _is_mapping_configured(m) -> bool:
    source_type = str(getattr(m, "source_type", "") or "").strip()
    if not source_type or source_type == "skip":
        return False
    value = str(getattr(m, "source_value", "") or "").strip()
    if source_type in {"field", "formula", "literal", "selector_map"}:
        return bool(value)
    return True


async def _sync_template_structure_and_mappings(
    db: Session,
    client,
    template_id: int,
    file_id: str,
) -> list[dict]:
    """
    Синхронизировать structureRead и набор маппингов.
    Существующие source_type/source_value сохраняем.
    """
    structure = await client.get_template_structure(file_id)
    repo.save_template_structure(db, template_id, structure)

    variables = _flatten_structure(structure.get("scheme", {}))
    existing = {m.variable_id: m for m in repo.get_mappings(db, template_id)}

    for var in variables:
        vid = str(var["id"])
        common = {
            "variable_name": var["name"],
            "variable_kind": var["kind"],
            "variable_type": var.get("type", ""),
            "parent_variable_id": str(var["parent_id"]) if var.get("parent_id") else None,
        }
        if vid in existing:
            repo.upsert_mapping(db, template_id, variable_id=vid, **common)
        else:
            repo.upsert_mapping(
                db,
                template_id,
                variable_id=vid,
                source_type="skip",
                source_value="",
                **common,
            )
    return variables
