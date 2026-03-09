#!/usr/bin/env python3
"""
Регистрация кнопки в Битрикс24.

Запускается ОДИН РАЗ после деплоя.
Добавляет кнопку «Создать документ Doczilla» в карточку сделки.

Использование:
    python scripts/register_bitrix_app.py

    # Удалить кнопку (при переустановке):
    python scripts/register_bitrix_app.py --unregister

Как это работает:
    Б24 позволяет зарегистрировать «placement» — точку встройки UI.
    Мы регистрируем placement типа CRM_DEAL_DETAIL_TAB (вкладка) или
    CRM_DEAL_TOOLBAR (кнопка в тулбаре).
    При нажатии Б24 открывает iframe с нашим /bitrix/widget.

Документация Б24:
    https://dev.1c-bitrix.ru/rest_help/application_embedding/
"""
import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from dotenv import load_dotenv
load_dotenv()

from app.core.config import get_settings

settings = get_settings()

# URL виджета — то, что Б24 откроет в iframe при нажатии кнопки
# При локальной разработке используй ngrok: https://xxxx.ngrok.io/bitrix/widget
WIDGET_URL = f"{settings.APP_PUBLIC_URL}/bitrix/widget"


async def register():
    """Зарегистрировать кнопку в Б24."""
    async with httpx.AsyncClient() as client:
        base = settings.BITRIX_WEBHOOK_URL.rstrip("/")

        # ── 1. Регистрация placement (кнопка в тулбаре карточки сделки) ──────
        print("Регистрируем placement CRM_DEAL_TOOLBAR...")
        res = await client.post(f"{base}/placement.bind.json", json={
            "PLACEMENT": "CRM_DEAL_TOOLBAR",     # кнопка в шапке сделки
            "HANDLER":   WIDGET_URL,
            "TITLE":     "📄 Создать документ",  # текст кнопки
            "DESCRIPTION": "Генерация PDF через Doczilla PRO",
            "OPTIONS": {
                "extranet": "N",   # не показывать экстранет-пользователям
            }
        })
        data = res.json()
        if data.get("result") is True:
            print("✅ Кнопка зарегистрирована в тулбаре сделки!")
        else:
            print(f"⚠️  Ответ Б24: {data}")

        # ── 2. Опционально: вкладка в карточке сделки ─────────────────────────
        # Раскомментируй если хочешь вкладку вместо/помимо кнопки
        #
        # print("Регистрируем placement CRM_DEAL_DETAIL_TAB...")
        # res2 = await client.post(f"{base}/placement.bind.json", json={
        #     "PLACEMENT": "CRM_DEAL_DETAIL_TAB",
        #     "HANDLER":   WIDGET_URL,
        #     "TITLE":     "Doczilla",
        # })
        # print(res2.json())

        # ── 3. Проверка — список зарегистрированных placement ─────────────────
        print("\nТекущие placement:")
        res3 = await client.post(f"{base}/placement.list.json")
        for p in res3.json().get("result", []):
            print(f"  • {p.get('placement')}: {p.get('handler')}")


async def unregister():
    """Удалить все зарегистрированные placement (для сброса)."""
    async with httpx.AsyncClient() as client:
        base = settings.BITRIX_WEBHOOK_URL.rstrip("/")

        res = await client.post(f"{base}/placement.list.json")
        placements = res.json().get("result", [])

        if not placements:
            print("Нет зарегистрированных placement.")
            return

        for p in placements:
            placement_id = p.get("id") or p.get("placement")
            res2 = await client.post(f"{base}/placement.unbind.json", json={
                "PLACEMENT": p.get("placement"),
                "HANDLER":   p.get("handler"),
            })
            print(f"Удалён: {p.get('placement')} → {res2.json()}")
        print("✅ Готово.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--unregister", action="store_true", help="Удалить кнопку из Б24")
    args = parser.parse_args()

    if args.unregister:
        asyncio.run(unregister())
    else:
        asyncio.run(register())
