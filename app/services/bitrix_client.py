"""
Клиент Битрикс24 REST API.

Режим авторизации:
  OAuth локального приложения (AUTH_ID / REFRESH_ID / DOMAIN).
"""
import logging
import base64
from datetime import datetime
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

    def __init__(self, domain: str | None = None, access_token: str | None = None):
        self._client = httpx.AsyncClient(timeout=30.0)
        # domain = портал Б24 (например crm-test.doczilla.pro)
        self._domain = self._normalize_domain(domain)
        # AUTH_ID текущего пользователя из BX24.getAuth() (если передан)
        self._provided_access_token = access_token

    async def close(self):
        await self._client.aclose()

    @staticmethod
    def _normalize_domain(domain: str | None) -> str | None:
        """Привести DOMAIN к виду portal.bitrix24.ru без протокола и пути."""
        if not domain:
            return None
        value = str(domain).strip()
        value = value.replace("https://", "").replace("http://", "")
        value = value.split("/", 1)[0]
        return value or None

    # ── OAuth helpers ─────────────────────────────────────────────────────────

    async def _get_access_token(self) -> str | None:
        """Получить OAuth-токен: из запроса или из БД с refresh при необходимости."""
        if self._provided_access_token:
            return self._provided_access_token

        if not self._domain:
            raise BitrixError("Не указан DOMAIN портала Битрикс24")

        from app.db.database import SessionLocal
        from app.db import repository as repo
        with SessionLocal() as db:
            token = repo.get_oauth_token(db, self._domain)
        if not token:
            raise BitrixError(
                f"OAuth-токен для портала '{self._domain}' не найден. Переустановите локальное приложение."
            )

        if token.expires_at < datetime.utcnow():
            token = await self._refresh_token(token)
        if not token:
            raise BitrixError(
                f"Не удалось обновить OAuth-токен для портала '{self._domain}'. Проверьте CLIENT_ID/CLIENT_SECRET."
            )
        return token.access_token

    async def _refresh_token(self, token):
        """Обновить истёкший токен через refresh_token."""
        if not settings.BITRIX_CLIENT_ID or not settings.BITRIX_CLIENT_SECRET:
            logger.warning("BITRIX_CLIENT_ID/BITRIX_CLIENT_SECRET не заданы, refresh невозможен")
            return None
        if not token.refresh_token:
            logger.warning("refresh_token отсутствует для domain=%s", token.domain)
            return None

        try:
            r = await self._client.get(
                "https://oauth.bitrix.info/oauth/token/",
                params={
                    "grant_type":    "refresh_token",
                    "client_id":     settings.BITRIX_CLIENT_ID,
                    "client_secret": settings.BITRIX_CLIENT_SECRET,
                    "refresh_token": token.refresh_token,
                }
            )
            data = r.json()
            if "access_token" in data:
                from app.db.database import SessionLocal
                from app.db import repository as repo
                with SessionLocal() as db:
                    return repo.save_oauth_token(
                        db,
                        token.domain,
                        data["access_token"],
                        data["refresh_token"],
                        int(data.get("expires_in", 3600)),
                        token.member_id,
                    )
        except Exception as e:
            logger.error("refresh_token failed: %s", e)
        return None

    # ── Внутренний helper ─────────────────────────────────────────────────────

    async def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        """
        Вызвать метод Битрикс24 REST API через OAuth локального приложения.
        """
        if not self._domain:
            raise BitrixError("Не указан DOMAIN портала для вызова REST API")

        access_token = await self._get_access_token()
        url = f"https://{self._domain}/rest/{method}.json"
        response = await self._client.post(
            url,
            params={"auth": access_token},
            json=params or {}
        )

        response.raise_for_status()
        data = response.json()

        if "error" in data:
            raise BitrixError(f"{data['error']}: {data.get('error_description', '')}")

        if raw:
            return data
        return data.get("result")

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Публичный вызов произвольного метода REST API Битрикс24."""
        return await self._call(method, params)

    async def call_raw(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Публичный вызов, возвращающий полный ответ (result/next/total/time)."""
        data = await self._call(method, params, raw=True)
        return data if isinstance(data, dict) else {"result": data}

    # ── CRM: Сделки ───────────────────────────────────────────────────────────

    async def get_deal(self, deal_id: int | str) -> dict:
        """Получить все поля сделки по ID."""
        result = await self._call("crm.deal.get", {"ID": deal_id})
        if not result:
            raise BitrixError(f"Сделка {deal_id} не найдена")
        return result

    async def get_lead(self, lead_id: int | str) -> dict:
        """Получить все поля лида по ID."""
        result = await self._call("crm.lead.get", {"ID": lead_id})
        if not result:
            raise BitrixError(f"Лид {lead_id} не найден")
        return result

    async def update_deal(self, deal_id: int | str, fields: dict[str, Any]) -> bool:
        """Обновить поля сделки. Возвращает True при успехе."""
        result = await self._call("crm.deal.update", {
            "ID": deal_id,
            "FIELDS": fields,
        })
        return bool(result)

    async def set_deal_doczilla_link(self, deal_id: int | str, link: str, *, multiple: bool = False) -> bool:
        """Записать ссылку на документ Doczilla в поле сделки."""
        value: Any = [link] if multiple else link
        return await self.update_deal(deal_id, {settings.BITRIX_DEAL_LINK_FIELD: value})

    async def set_deal_field(self, deal_id: int | str, field_code: str, value: Any, *, multiple: bool = False) -> bool:
        """Записать значение в произвольное поле сделки."""
        code = str(field_code or "").strip()
        if not code:
            raise BitrixError("Не указан код поля сделки")
        if multiple and not isinstance(value, list):
            value = [value]
        return await self.update_deal(deal_id, {code: value})

    async def set_deal_file_field(
        self,
        deal_id: int | str,
        field_code: str,
        filename: str,
        content: bytes,
        *,
        multiple: bool = False,
    ) -> bool:
        """
        Загрузить файл в пользовательское FILE-поле сделки через crm.deal.update.
        """
        code = str(field_code or "").strip()
        if not code:
            raise BitrixError("Не указан код FILE-поля сделки")
        b64 = base64.b64encode(content).decode()
        file_value: dict[str, Any] = {"fileData": [filename, b64]}
        value: Any = [file_value] if multiple else file_value
        return await self.update_deal(deal_id, {code: value})

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
                "ENTITY_ID":   deal_id,
                "ENTITY_TYPE": "deal",
                "COMMENT":     text,
            }
        })

    # ── Диск (опционально: загрузка PDF) ─────────────────────────────────────

    async def upload_file_to_disk(
        self,
        folder_id: int | str,
        filename: str,
        content: bytes,
    ) -> dict:
        """Загрузить файл в папку на Диске Б24."""
        import base64
        result = await self._call("disk.folder.uploadfile", {
            "id":          folder_id,
            "data":        {"NAME": filename},
            "fileContent": base64.b64encode(content).decode(),
        })
        return result or {}
