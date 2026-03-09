#!/usr/bin/env python3
"""
Утилита: inspect_template.py

Выводит все переменные шаблона Doczilla — нужно запустить ПЕРЕД заполнением
mapping.py, чтобы узнать точные имена переменных.

Использование:
    python scripts/inspect_template.py --file-id RECORD-ID-ШАБЛОНА
    python scripts/inspect_template.py --link SVI8PZ

    # Вывести список всех шаблонов в разделе:
    python scripts/inspect_template.py --list-templates --section-id SECTION-ID
"""
import asyncio
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from dotenv import load_dotenv
load_dotenv()

from app.core.config import get_settings
from app.core.session import get_session
from app.services.doczilla_client import DoczillaClient

settings = get_settings()


async def main():
    parser = argparse.ArgumentParser(description="Просмотр переменных шаблона Doczilla")
    parser.add_argument("--file-id", help="recordId шаблона в Doczilla")
    parser.add_argument("--link", help="link шаблона (короткий код)")
    parser.add_argument("--list-templates", action="store_true", help="Показать список шаблонов")
    parser.add_argument("--section-id", help="ID раздела для --list-templates")
    args = parser.parse_args()

    client = DoczillaClient()
    try:
        if args.list_templates:
            section_id = args.section_id or settings.DOCZILLA_TEMPLATES_SECTION_ID
            if not section_id:
                print("❌ Укажите --section-id или задайте DOCZILLA_TEMPLATES_SECTION_ID в .env")
                return
            templates = await client.get_templates(section_id)
            print(f"\n📋 Найдено шаблонов: {len(templates)}\n")
            print(f"{'Имя':<40} {'recordId':<38} {'link':<8} {'folder'}")
            print("-" * 120)
            for t in templates:
                folder = t.get("folderName") or "(root)"
                print(f"{t.get('name', ''):<40} {t.get('recordId', ''):<38} {t.get('link', ''):<8} {folder}")
            return

        if not args.file_id and not args.link:
            parser.print_help()
            return

        # Получить структуру шаблона
        file_id = args.file_id or ""
        print(f"\n🔍 Запрашиваем структуру шаблона: {file_id or args.link}...\n")
        structure = await client.get_template_structure(file_id)

        print("📄 ПОЛНАЯ СТРУКТУРА (JSON):")
        print(json.dumps(structure, ensure_ascii=False, indent=2))

        # Попробуем выделить переменные для удобства
        variables = _extract_variables(structure)
        if variables:
            print("\n✅ ПЕРЕМЕННЫЕ ШАБЛОНА (для mapping.py):")
            print(f"{'Имя переменной':<35} {'Тип':<15} {'Описание'}")
            print("-" * 70)
            for v in variables:
                print(f"{v.get('name', ''):<35} {v.get('type', ''):<15} {v.get('title', '')}")

            print("\n💡 Скопируйте в mapping.py:")
            print('variables={')
            for v in variables:
                name = v.get("name", "")
                print(f'    "{name}": "deal.",  # {v.get("title", "")}')
            print('}')

    finally:
        await client.close()


def _extract_variables(structure: dict) -> list[dict]:
    """Попытаться извлечь список переменных из структуры шаблона."""
    # Doczilla может вернуть переменные в разных ключах — адаптируйте под вашу версию
    for key in ("variables", "fields", "items", "data"):
        if key in structure and isinstance(structure[key], list):
            return structure[key]
    return []


if __name__ == "__main__":
    asyncio.run(main())
