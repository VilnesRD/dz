#!/usr/bin/env python3
"""
Регистрация placement в Битрикс24 для ЛОКАЛЬНОГО OAuth-приложения.

Скрипт НЕ использует входящий вебхук.
Токены берутся из БД (oauth_tokens), которые сохраняются при /install.

Использование:
    python3 scripts/register_bitrix_app.py
    python3 scripts/register_bitrix_app.py --domain crm-test.doczilla.pro
    python3 scripts/register_bitrix_app.py --unregister
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db import repository as repo
from app.services.bitrix_client import BitrixClient

settings = get_settings()
WIDGET_URL = f"{settings.APP_PUBLIC_URL.rstrip('/')}/bitrix/widget"
TARGET_PLACEMENT = "CRM_DEAL_DETAIL_TOOLBAR"
LEAD_USERFIELD_HANDLER_URL = f"{settings.APP_PUBLIC_URL.rstrip('/')}/bitrix/lead-userfield"
DEFAULT_LEAD_USERFIELD_TYPE = "doczilla_field"
DEFAULT_LEAD_USERFIELD_CODE = "DOCZILLA"


def iter_placements(raw_result):
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
        # Формат одного объекта
        if raw_result.get("placement") or raw_result.get("PLACEMENT"):
            placement = raw_result.get("placement") or raw_result.get("PLACEMENT")
            handler = raw_result.get("handler") or raw_result.get("HANDLER")
            yield str(placement), (str(handler) if handler else None)
            return

        # Формат map: {"CRM_DEAL_DETAIL_TOOLBAR": [...handlers...]}
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


def resolve_domain(cli_domain: str | None) -> str:
    with SessionLocal() as db:
        token = repo.get_oauth_token(db, cli_domain) if cli_domain else repo.get_latest_oauth_token(db)
    if not token:
        raise RuntimeError(
            "OAuth-токен не найден. Сначала переустановите локальное приложение в Битрикс24."
        )
    return token.domain


async def register(domain: str):
    """Зарегистрировать placement через OAuth local app."""
    client = BitrixClient(domain=domain)
    try:
        current = await client.call("placement.list")
        normalized = list(iter_placements(current))
        if not normalized:
            print(f"placement.list (raw): {current}")
        for placement, handler in normalized:
            if placement != TARGET_PLACEMENT:
                continue
            payload = {"PLACEMENT": placement}
            if handler:
                payload["HANDLER"] = handler
            result = await client.call("placement.unbind", payload)
            print(f"Удалён старый placement {placement} ({handler or 'no-handler'}): {result}")

        bind_result = await client.call("placement.bind", {
            "PLACEMENT": TARGET_PLACEMENT,
            "HANDLER": WIDGET_URL,
            "TITLE": "📄 Создать документ",
            "DESCRIPTION": "Генерация PDF через Doczilla PRO",
        })
        print(f"placement.bind {TARGET_PLACEMENT}: {bind_result}")

        current = await client.call("placement.list")
        normalized = list(iter_placements(current))
        if not normalized:
            print(f"placement.list (raw): {current}")
        print("\nТекущие placement:")
        for placement, handler in normalized:
            print(f"  • {placement}: {handler or 'no-handler'}")
    finally:
        await client.close()


async def unregister(domain: str):
    """Удалить placement local app."""
    client = BitrixClient(domain=domain)
    try:
        current = await client.call("placement.list")
        normalized = list(iter_placements(current))
        if not normalized:
            print(f"placement.list (raw): {current}")
        found = False
        for placement, handler in normalized:
            if placement != TARGET_PLACEMENT:
                continue
            found = True
            payload = {"PLACEMENT": placement}
            if handler:
                payload["HANDLER"] = handler
            result = await client.call("placement.unbind", payload)
            print(f"Удалён {placement} ({handler or 'no-handler'}): {result}")
        if not found:
            print("Нет зарегистрированных placement для удаления.")
    finally:
        await client.close()


def _normalize_lead_field_code(code: str) -> str:
    value = str(code or "").strip().upper()
    value = "".join(ch for ch in value if ch.isalnum() or ch == "_")
    value = value.strip("_")
    if value.startswith("UF_CRM_"):
        value = value[7:]
    if not value:
        value = DEFAULT_LEAD_USERFIELD_CODE
    # В примере Б24 ограничение 20 символов с учётом префикса UF_CRM_
    max_tail_len = max(1, 20 - len("UF_CRM_"))
    return value[:max_tail_len]


def _is_already_exists_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    markers = ["already", "exists", "уже", "duplicate", "дубли"]
    return any(m in text for m in markers)


async def register_lead_userfield(
    domain: str,
    *,
    user_type_id: str,
    field_code: str,
    title: str,
):
    """
    Зарегистрировать пользовательский тип и поле лида для handler USERFIELD_TYPE.
    """
    user_type = str(user_type_id or DEFAULT_LEAD_USERFIELD_TYPE).strip()
    if not user_type:
        raise RuntimeError("Не указан USER_TYPE_ID")
    field_code = _normalize_lead_field_code(field_code)
    full_field_code = f"UF_CRM_{field_code}"
    client = BitrixClient(domain=domain)
    try:
        print(f"Регистрация userfieldtype.add ({user_type}) -> {LEAD_USERFIELD_HANDLER_URL}")
        try:
            res = await client.call("userfieldtype.add", {
                "USER_TYPE_ID": user_type,
                "HANDLER": LEAD_USERFIELD_HANDLER_URL,
                "TITLE": title,
                "DESCRIPTION": "Doczilla lead widget field type",
            })
            print(f"userfieldtype.add: {res}")
        except Exception as e:
            if _is_already_exists_error(e):
                print(f"userfieldtype.add: тип уже существует ({e})")
            else:
                raise

        for check_code in (field_code, full_field_code):
            existing = await client.call("crm.lead.userfield.list", {"filter": {"FIELD_NAME": check_code}})
            exists_id = None
            if isinstance(existing, list) and existing:
                first = existing[0]
                if isinstance(first, dict):
                    exists_id = str(first.get("ID") or "").strip() or None
            if exists_id:
                print(f"crm.lead.userfield.add: поле уже существует (ID={exists_id}, FIELD_NAME={check_code})")
                return

        candidates = [field_code, full_field_code]
        last_error = None
        for candidate in candidates:
            try:
                print(f"Регистрация crm.lead.userfield.add (FIELD_NAME={candidate}, USER_TYPE_ID={user_type})")
                res = await client.call("crm.lead.userfield.add", {
                    "fields": {
                        "USER_TYPE_ID": user_type,
                        "FIELD_NAME": candidate,
                        "XML_ID": field_code,
                        "MANDATORY": "N",
                        "SHOW_IN_LIST": "Y",
                        "EDIT_IN_LIST": "Y",
                        "EDIT_FORM_LABEL": title,
                        "LIST_COLUMN_LABEL": title,
                        "LIST_FILTER_LABEL": title,
                        "SETTINGS": {},
                    }
                })
                print(f"crm.lead.userfield.add: {res}")
                return
            except Exception as e:
                last_error = e
                print(f"crm.lead.userfield.add ({candidate}): {e}")
        if last_error:
            raise last_error
    finally:
        await client.close()


async def unregister_lead_userfield(
    domain: str,
    *,
    user_type_id: str,
    field_code: str,
):
    """
    Удалить поле лида и тип поля USERFIELD_TYPE (если поддерживается порталом).
    """
    user_type = str(user_type_id or DEFAULT_LEAD_USERFIELD_TYPE).strip()
    field_code = _normalize_lead_field_code(field_code)
    full_field_code = f"UF_CRM_{field_code}"
    client = BitrixClient(domain=domain)
    try:
        found = False
        for check_code in (field_code, full_field_code):
            existing = await client.call("crm.lead.userfield.list", {"filter": {"FIELD_NAME": check_code}})
            if isinstance(existing, list) and existing:
                found = True
                for row in existing:
                    if not isinstance(row, dict):
                        continue
                    uf_id = str(row.get("ID") or "").strip()
                    if not uf_id:
                        continue
                    try:
                        res = await client.call("crm.lead.userfield.delete", {"id": uf_id})
                        print(f"crm.lead.userfield.delete id={uf_id}: {res}")
                    except Exception as e:
                        print(f"crm.lead.userfield.delete id={uf_id}: {e}")
        if not found:
            print(f"crm.lead.userfield.list: поле {field_code}/{full_field_code} не найдено")

        if user_type:
            try:
                res = await client.call("userfieldtype.delete", {"USER_TYPE_ID": user_type})
                print(f"userfieldtype.delete {user_type}: {res}")
            except Exception as e:
                print(f"userfieldtype.delete {user_type}: {e}")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", help="Домен портала Б24 (например crm-test.doczilla.pro)")
    parser.add_argument("--unregister", action="store_true", help="Удалить placement")
    parser.add_argument("--register-lead-userfield", action="store_true", help="Создать userfieldtype + поле лида")
    parser.add_argument("--unregister-lead-userfield", action="store_true", help="Удалить поле лида + userfieldtype")
    parser.add_argument("--lead-userfield-type", default=DEFAULT_LEAD_USERFIELD_TYPE, help=f"USER_TYPE_ID (по умолчанию: {DEFAULT_LEAD_USERFIELD_TYPE})")
    parser.add_argument("--lead-field-code", default=DEFAULT_LEAD_USERFIELD_CODE, help=f"FIELD_NAME для crm.lead.userfield.add (по умолчанию: {DEFAULT_LEAD_USERFIELD_CODE})")
    parser.add_argument("--lead-field-title", default="Doczilla документ", help="Заголовок пользовательского поля лида")
    args = parser.parse_args()

    domain = resolve_domain(args.domain)
    print(f"Портал: {domain}")
    if args.unregister:
        asyncio.run(unregister(domain))
    elif args.register_lead_userfield:
        asyncio.run(register_lead_userfield(
            domain,
            user_type_id=args.lead_userfield_type,
            field_code=args.lead_field_code,
            title=args.lead_field_title,
        ))
    elif args.unregister_lead_userfield:
        asyncio.run(unregister_lead_userfield(
            domain,
            user_type_id=args.lead_userfield_type,
            field_code=args.lead_field_code,
        ))
    else:
        asyncio.run(register(domain))
