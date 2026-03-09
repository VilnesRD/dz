"""
Клиент Битрикс24 REST API.

Поддерживает два режима авторизации:
  1. OAuth (приоритет) — если передан domain и в БД есть токены
  2. Входящий вебхук   — fallback через BITRIX_WEBHOOK_URL из конфига
"""
import logging
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

    def __init__(self, domain: str = None):
        self._client = httpx.AsyncClient(timeout=30.0)
        # domain = портал Б24 (например crm-test.doczilla.pro)
        # если None — используем вебхук из конфига
        self._domain = domain
        self._base = settings.BITRIX_WEBHOOK_URL.rstrip("/")

    async def close(self):
        await self._client.aclose()

    # ── OAuth helpers ─────────────────────────────────────────────────────────

    async def _get_access_token(self) -> str | None:
        """Получить актуальный OAuth-токен, обновив если истёк."""
        if not self._domain:
            return None
        from app.db.database import SessionLocal
        from app.db import repository as repo
        with SessionLocal() as db:
            token = repo.get_oauth_token(db, self._domain)
        if not token:
            return None
        if token.expires_at < datetime.utcnow():
            token = await self._refresh_token(token)
        return token.access_token if token else None

    async def _refresh_token(self, token):
        """Обновить истёкший токен через refresh_token."""
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

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """
        Вызвать метод Битрикс24 REST API.
        Если есть OAuth-токен — использует его, иначе вебхук.
        """
        access_token = await self._get_access_token()

        if access_token and self._domain:
            url = f"https://{self._domain}/rest/{method}.json"
            response = await self._client.post(
                url,
                params={"auth": access_token},
                json=params or {}
            )
        else:
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
        """Записать ссылку на документ Doczilla в поле сделки."""
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
