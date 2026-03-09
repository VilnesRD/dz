"""
Клиент Битрикс24 REST API.

Используем исходящий вебхук Б24 (не OAuth), поэтому авторизация
встроена прямо в BITRIX_WEBHOOK_URL из конфига.

Покрываем только методы, нужные для основного сценария:
    - crm.deal.get         — прочитать данные сделки
    - crm.contact.get      — прочитать данные контакта
    - crm.company.get      — прочитать данные компании
    - crm.deal.update      — записать поле в сделку (ссылка на PDF)
    - crm.activity.add     — добавить активность/комментарий в ленту
    - disk.folder.uploadfile — загрузить PDF в Диск Б24 (если потребуется)
"""
import logging
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class BitrixError(Exception):
    """Ошибка от Битрикс24 REST API."""


class BitrixClient:
    """
    Асинхронный клиент Битрикс24 REST API.
    Создаётся один раз при старте FastAPI и внедряется через Depends.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        # Базовый URL вебхука: https://portal.bitrix24.ru/rest/1/xxxx
        self._base = settings.BITRIX_WEBHOOK_URL.rstrip("/")

    async def close(self):
        await self._client.aclose()

    # ── Внутренний helper ─────────────────────────────────────────────────────

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """
        Вызвать метод Битрикс24 REST API.
        Документация: https://dev.1c-bitrix.ru/rest_help/
        """
        url = f"{self._base}/{method}.json"
        response = await self._client.post(url, json=params or {})
        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise BitrixError(f"{data['error']}: {data.get('error_description', '')}")

        return data.get("result")

    # ── CRM: Сделки ───────────────────────────────────────────────────────────

    async def get_deal(self, deal_id: int | str) -> dict:
        """Получить все поля сделки по ID."""
        result = await self._call("crm.deal.get", {"ID": deal_id})
        if not result:
            raise BitrixError(f"Сделка {deal_id} не найдена")
        return result

    async def update_deal(self, deal_id: int | str, fields: dict[str, Any]) -> bool:
        """Обновить поля сделки. Возвращает True при успехе."""
        result = await self._call("crm.deal.update", {
            "ID": deal_id,
            "FIELDS": fields,
        })
        return bool(result)

    async def set_deal_doczilla_link(self, deal_id: int | str, link: str) -> bool:
        """
        Записать ссылку на документ Doczilla в специальное поле сделки.
        Поле задаётся в конфиге: BITRIX_DEAL_LINK_FIELD (по умолчанию UF_CRM_DOCZILLA_LINK).
        Это поле нужно создать в Б24 вручную: Тип = "Строка".
        """
        return await self.update_deal(deal_id, {settings.BITRIX_DEAL_LINK_FIELD: link})

    # ── CRM: Контакты ─────────────────────────────────────────────────────────

    async def get_contact(self, contact_id: int | str) -> dict:
        """Получить данные контакта."""
        result = await self._call("crm.contact.get", {"ID": contact_id})
        if not result:
            raise BitrixError(f"Контакт {contact_id} не найден")
        return result

    async def get_deal_contacts(self, deal_id: int | str) -> list[dict]:
        """Получить список контактов, привязанных к сделке."""
        result = await self._call("crm.deal.contact.items.get", {"ID": deal_id})
        return result or []

    # ── CRM: Компании ─────────────────────────────────────────────────────────

    async def get_company(self, company_id: int | str) -> dict:
        """Получить данные компании."""
        result = await self._call("crm.company.get", {"ID": company_id})
        if not result:
            raise BitrixError(f"Компания {company_id} не найдена")
        return result

    # ── Лента активности ──────────────────────────────────────────────────────

    async def add_deal_comment(self, deal_id: int | str, text: str) -> None:
        """Добавить комментарий в ленту сделки (видно в таймлайне)."""
        await self._call("crm.timeline.comment.add", {
            "fields": {
                "ENTITY_ID": deal_id,
                "ENTITY_TYPE": "deal",
                "COMMENT": text,
            }
        })

    # ── Диск (опционально: загрузка PDF) ─────────────────────────────────────

    async def upload_file_to_disk(
        self,
        folder_id: int | str,
        filename: str,
        content: bytes,
    ) -> dict:
        """
        Загрузить файл в папку на Диске Б24.
        Возвращает объект файла с полями: ID, NAME, DOWNLOAD_URL, ...
        """
        import base64
        result = await self._call("disk.folder.uploadfile", {
            "id": folder_id,
            "data": {"NAME": filename},
            "fileContent": base64.b64encode(content).decode(),
        })
        return result or {}
