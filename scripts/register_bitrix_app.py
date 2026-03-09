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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", help="Домен портала Б24 (например crm-test.doczilla.pro)")
    parser.add_argument("--unregister", action="store_true", help="Удалить placement")
    args = parser.parse_args()

    domain = resolve_domain(args.domain)
    print(f"Портал: {domain}")
    if args.unregister:
        asyncio.run(unregister(domain))
    else:
        asyncio.run(register(domain))
