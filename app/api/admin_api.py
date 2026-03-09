"""
REST API для админ-панели.

Эндпоинты:
  POST /admin/auth/login          — получить JWT-токен
  GET  /admin/auth/me             — проверить токен

  GET    /admin/templates         — список шаблонов
  POST   /admin/templates         — создать шаблон
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

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

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
    active: bool = True


@router.get("/templates")
async def list_templates(db: Session = Depends(get_db),
                         _=Depends(current_user)):
    templates = repo.list_templates(db)
    return [_template_to_dict(t) for t in templates]


@router.post("/templates", status_code=201)
async def create_template(body: TemplateIn,
                          db: Session = Depends(get_db),
                          _=Depends(current_user)):
    if repo.get_template_by_key(db, body.key):
        raise HTTPException(400, f"Шаблон с ключом '{body.key}' уже существует")
    t = repo.create_template(db, **body.model_dump())
    return _template_to_dict(t)


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
    t = _get_or_404(db, tid)
    # Если file_id изменился — сбросить кэш структуры
    if t.doczilla_file_id != body.doczilla_file_id:
        repo.update_template(db, tid, structure_json=None, structure_updated_at=None)
    t = repo.update_template(db, tid, **body.model_dump())
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
        structure = await client.get_template_structure(t.doczilla_file_id)
    finally:
        await client.close()

    repo.save_template_structure(db, tid, structure)

    # Авто-создать заготовки маппингов для всех переменных из структуры
    variables = _flatten_structure(structure.get("scheme", {}))
    for var in variables:
        repo.upsert_mapping(db, tid,
            variable_id   = str(var["id"]),
            variable_name = var["name"],
            variable_kind = var["kind"],
            variable_type = var.get("type", ""),
            source_type   = "skip",
            source_value  = "",
            parent_variable_id = str(var["parent_id"]) if var.get("parent_id") else None,
        )

    return {"variables": variables, "count": len(variables)}


# ── Mappings ──────────────────────────────────────────────────────────────────

class MappingItem(BaseModel):
    variable_id: str
    source_type: str   # field | formula | literal | selector_map | skip
    source_value: str = ""


@router.get("/templates/{tid}/mappings")
async def get_mappings(tid: int, db: Session = Depends(get_db),
                       _=Depends(current_user)):
    _get_or_404(db, tid)
    mappings = repo.get_mappings(db, tid)
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
    try:
        # Поля сделки
        try:
            deal_fields = await client.call("crm.deal.fields")
            for fname, finfo in (deal_fields or {}).items():
                fields.append({
                    "path": f"deal.{fname}",
                    "label": f"Сделка: {finfo.get('title', fname)}",
                    "type": finfo.get("type", ""),
                })
        except Exception as e:
            log.warning("crm.deal.fields: %s", e)

        # Поля контакта
        try:
            contact_fields = await client.call("crm.contact.fields")
            for fname, finfo in (contact_fields or {}).items():
                fields.append({
                    "path": f"contact.{fname}",
                    "label": f"Контакт: {finfo.get('title', fname)}",
                    "type": finfo.get("type", ""),
                })
        except Exception as e:
            log.warning("crm.contact.fields: %s", e)

        # Поля компании
        try:
            company_fields = await client.call("crm.company.fields")
            for fname, finfo in (company_fields or {}).items():
                fields.append({
                    "path": f"company.{fname}",
                    "label": f"Компания: {finfo.get('title', fname)}",
                    "type": finfo.get("type", ""),
                })
        except Exception as e:
            log.warning("crm.company.fields: %s", e)
    except BitrixError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))
    finally:
        await client.close()

    return {"fields": fields}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_404(db: Session, tid: int):
    t = repo.get_template(db, tid)
    if not t:
        raise HTTPException(404, "Шаблон не найден")
    return t


def _template_to_dict(t) -> dict:
    return {
        "id": t.id, "key": t.key, "name": t.name,
        "doczilla_file_id": t.doczilla_file_id,
        "doczilla_link": t.doczilla_link,
        "doczilla_folder_id": t.doczilla_folder_id,
        "doc_name_template": t.doc_name_template,
        "active": t.active,
        "has_structure": bool(t.structure_json),
        "structure_updated_at": t.structure_updated_at.isoformat() if t.structure_updated_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "mappings_count": len(t.mappings),
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
