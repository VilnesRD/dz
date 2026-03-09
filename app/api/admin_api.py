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
from typing import Annotated, Any

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
    bitrix_deal_link_field: str = settings.BITRIX_DEAL_LINK_FIELD
    bitrix_deal_pdf_field: str = ""
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
                bitrix_deal_link_field=settings.BITRIX_DEAL_LINK_FIELD,
                bitrix_deal_pdf_field="",
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
    stats = {"deal": 0, "contact": 0, "company": 0}
    custom_stats = {"deal": 0, "contact": 0, "company": 0}
    uf_label_stats = {"deal": 0, "contact": 0, "company": 0}
    errors: list[str] = []
    try:
        # Названия кастомных UF_* полей берём из userfield.list
        deal_uf_labels: dict[str, str] = {}
        contact_uf_labels: dict[str, str] = {}
        company_uf_labels: dict[str, str] = {}
        try:
            deal_uf_rows = await _call_bitrix_list_all(client, "crm.deal.userfield.list")
            deal_uf_labels = _build_userfield_label_map(deal_uf_rows)
            uf_label_stats["deal"] = len(deal_uf_labels)
        except Exception as e:
            log.warning("crm.deal.userfield.list: %s", e)
            errors.append(f"deal.userfield: {e}")
        try:
            contact_uf_rows = await _call_bitrix_list_all(client, "crm.contact.userfield.list")
            contact_uf_labels = _build_userfield_label_map(contact_uf_rows)
            uf_label_stats["contact"] = len(contact_uf_labels)
        except Exception as e:
            log.warning("crm.contact.userfield.list: %s", e)
            errors.append(f"contact.userfield: {e}")
        try:
            company_uf_rows = await _call_bitrix_list_all(client, "crm.company.userfield.list")
            company_uf_labels = _build_userfield_label_map(company_uf_rows)
            uf_label_stats["company"] = len(company_uf_labels)
        except Exception as e:
            log.warning("crm.company.userfield.list: %s", e)
            errors.append(f"company.userfield: {e}")

        # Поля сделки
        deal_fields = await _load_crm_entity_fields(
            client=client,
            legacy_method="crm.deal.fields",
            item_entity_type_id=2,
            log_key="deal",
            errors=errors,
        )
        for fname, finfo in deal_fields.items():
            title = str(finfo.get("title", fname))
            if str(fname).upper().startswith("UF_"):
                title = deal_uf_labels.get(str(fname), title)
            fields.append({
                "path": f"deal.{fname}",
                "label": f"Сделка: {title}",
                "type": finfo.get("type", ""),
            })
        stats["deal"] = len(deal_fields)
        custom_stats["deal"] = len([k for k in deal_fields.keys() if str(k).upper().startswith("UF_")])

        # Поля контакта
        contact_fields = await _load_crm_entity_fields(
            client=client,
            legacy_method="crm.contact.fields",
            item_entity_type_id=3,
            log_key="contact",
            errors=errors,
        )
        for fname, finfo in contact_fields.items():
            title = str(finfo.get("title", fname))
            if str(fname).upper().startswith("UF_"):
                title = contact_uf_labels.get(str(fname), title)
            fields.append({
                "path": f"contact.{fname}",
                "label": f"Контакт: {title}",
                "type": finfo.get("type", ""),
            })
        stats["contact"] = len(contact_fields)
        custom_stats["contact"] = len([k for k in contact_fields.keys() if str(k).upper().startswith("UF_")])

        # Поля компании
        company_fields = await _load_crm_entity_fields(
            client=client,
            legacy_method="crm.company.fields",
            item_entity_type_id=4,
            log_key="company",
            errors=errors,
        )
        for fname, finfo in company_fields.items():
            title = str(finfo.get("title", fname))
            if str(fname).upper().startswith("UF_"):
                title = company_uf_labels.get(str(fname), title)
            fields.append({
                "path": f"company.{fname}",
                "label": f"Компания: {title}",
                "type": finfo.get("type", ""),
            })
        stats["company"] = len(company_fields)
        custom_stats["company"] = len([k for k in company_fields.keys() if str(k).upper().startswith("UF_")])
    except BitrixError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    finally:
        await client.close()

    fields.sort(key=lambda x: x.get("path", ""))
    log.info(
        "bitrix fields loaded: deal=%d contact=%d company=%d total=%d | custom: deal=%d contact=%d company=%d | uf labels: deal=%d contact=%d company=%d",
        stats["deal"], stats["contact"], stats["company"], len(fields),
        custom_stats["deal"], custom_stats["contact"], custom_stats["company"],
        uf_label_stats["deal"], uf_label_stats["contact"], uf_label_stats["company"],
    )
    return {
        "fields": fields,
        "meta": {
            "stats": stats,
            "custom_stats": custom_stats,
            "uf_label_stats": uf_label_stats,
            "errors": errors,
        },
    }


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
        "bitrix_deal_link_field": t.bitrix_deal_link_field or "",
        "bitrix_deal_pdf_field": t.bitrix_deal_pdf_field or "",
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
    if isinstance(value, dict):
        for key in ("ru", "ru_RU", "en", "en_US"):
            val = value.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for val in value.values():
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


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
        if not isinstance(meta, dict):
            meta = {}
        existing = item_fields_legacy.get(legacy_code)
        # Предпочитаем запись, где есть заголовок
        if not existing or (not existing.get("title") and meta.get("title")):
            item_fields_legacy[legacy_code] = dict(meta)

    merged = {str(k): dict(v) for k, v in legacy_fields.items() if isinstance(v, dict)}
    for code, meta in item_fields_legacy.items():
        if code not in merged:
            merged[code] = dict(meta)
            continue
        # Обогащаем legacy метаданные (title/type), не ломая обратную совместимость по ключам
        if not merged[code].get("title") and meta.get("title"):
            merged[code]["title"] = meta.get("title")
        if not merged[code].get("type") and meta.get("type"):
            merged[code]["type"] = meta.get("type")

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
    if text.upper() == text:
        return text

    # camelCase -> snake_case
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    # letter/number boundaries: parentId2 -> parent_id_2
    snake = re.sub(r"([A-Za-z])([0-9])", r"\1_\2", snake)
    snake = re.sub(r"([0-9])([A-Za-z])", r"\1_\2", snake)
    snake = snake.replace("-", "_")
    snake = re.sub(r"_+", "_", snake).strip("_")
    return snake.upper()


def _build_userfield_label_map(payload: Any) -> dict[str, str]:
    """
    Преобразовать ответ crm.*.userfield.list в map FIELD_NAME -> человекочитаемое имя.
    """
    if not isinstance(payload, list):
        return {}
    out: dict[str, str] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        code = str(row.get("FIELD_NAME") or "").strip()
        if not code:
            continue
        label = (
            _extract_localized_label(row.get("EDIT_FORM_LABEL"))
            or _extract_localized_label(row.get("LIST_COLUMN_LABEL"))
            or _extract_localized_label(row.get("LIST_FILTER_LABEL"))
            or code
        )
        out[code] = label
    return out


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
