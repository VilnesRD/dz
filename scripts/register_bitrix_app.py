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
        current = await client.call("placement.list") or []
        for item in current:
            if item.get("placement") != TARGET_PLACEMENT:
                continue
            result = await client.call("placement.unbind", {
                "PLACEMENT": item.get("placement"),
                "HANDLER": item.get("handler"),
            })
            print(f"Удалён старый placement {item.get('placement')} ({item.get('handler')}): {result}")

        bind_result = await client.call("placement.bind", {
            "PLACEMENT": TARGET_PLACEMENT,
            "HANDLER": WIDGET_URL,
            "TITLE": "📄 Создать документ",
            "DESCRIPTION": "Генерация PDF через Doczilla PRO",
        })
        print(f"placement.bind {TARGET_PLACEMENT}: {bind_result}")

        current = await client.call("placement.list") or []
        print("\nТекущие placement:")
        for item in current:
            print(f"  • {item.get('placement')}: {item.get('handler')}")
    finally:
        await client.close()


async def unregister(domain: str):
    """Удалить placement local app."""
    client = BitrixClient(domain=domain)
    try:
        current = await client.call("placement.list") or []
        found = False
        for item in current:
            if item.get("placement") != TARGET_PLACEMENT:
                continue
            found = True
            result = await client.call("placement.unbind", {
                "PLACEMENT": item.get("placement"),
                "HANDLER": item.get("handler"),
            })
            print(f"Удалён {item.get('placement')} ({item.get('handler')}): {result}")
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
