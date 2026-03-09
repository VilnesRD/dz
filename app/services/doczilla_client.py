"""
Клиент Doczilla PRO API v3.1.0.

Все методы — тонкие обёртки над единственным эндпоинтом POST /request.json.
При ошибке аутентификации автоматически повторяет signin один раз (retry=1).

Основной сценарий (генерация PDF из шаблона):
    1. get_template_structure()  — получить переменные шаблона
    2. create_docz()             — создать анкету из шаблона
    3. fill_docz()               — заполнить переменные данными из CRM
    4. get_document_pdf()        — скачать PDF
    5. get_document_info()       — получить ссылку на документ в Doczilla

Опционально:
    6. signout()                 — закрыть сессию (вызывается при shutdown)
"""
import logging
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.session import get_session, invalidate_session

logger = logging.getLogger(__name__)
settings = get_settings()

# Единственный эндпоинт Doczilla API
_ENDPOINT = f"{settings.DOCZILLA_BASE_URL}/request.json"


class DoczillaError(Exception):
    """Ошибка от Doczilla API (success=false)."""
    def __init__(self, messages: list[dict]):
        texts = [m.get("text", "") for m in messages]
        super().__init__(" | ".join(texts))
        self.messages = messages


class DoczillaClient:
    """
    Асинхронный клиент Doczilla PRO.
    Используйте как контекстный менеджер или создайте один экземпляр
    при старте приложения и передавайте через DI FastAPI.
    """

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self._client.aclose()

    # ── Внутренние helpers ────────────────────────────────────────────────────

    async def _request(self, params: dict[str, Any], _retry: bool = True) -> dict:
        """
        Выполнить запрос к /request.json с автоматическим управлением сессией.
        При ошибке аутентификации — сбрасывает сессию и повторяет один раз.
        """
        session = await get_session(self._client)
        params["session"] = session

        response = await self._client.post(_ENDPOINT, data=params)
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            messages = data.get("info", {}).get("messages", [])
            # Проверяем, не истекла ли сессия
            error_texts = " ".join(m.get("text", "") for m in messages).lower()
            if _retry and ("session" in error_texts or "signin" in error_texts or "auth" in error_texts):
                logger.warning("Doczilla: сессия устарела, обновляем...")
                await invalidate_session()
                return await self._request(params, _retry=False)
            raise DoczillaError(messages)

        return data

    # ── 1. Пользователи ───────────────────────────────────────────────────────

    async def signout(self) -> None:
        """Закрыть текущую сессию (вызывать при остановке сервиса)."""
        try:
            session = await get_session(self._client)
            await self._client.post(_ENDPOINT, data={"request": "signout", "session": session})
            await invalidate_session()
            logger.info("Doczilla: сессия закрыта")
        except Exception as e:
            logger.warning("Doczilla signout error: %s", e)

    # ── 3.1 Файловая система ──────────────────────────────────────────────────

    @staticmethod
    def _is_folder(item: dict) -> bool:
        """Нормализовать флаг isFolder из разных форматов (bool/str/int)."""
        kind = str(item.get("type") or "").strip().lower()
        if kind in {"folder", "dir", "directory"}:
            return True
        value = item.get("isFolder")
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "y")
        return False

    async def _read_workspace(
        self,
        section_id: str,
        folder_id: str,
        fields: str,
        sort: str,
        filter_expr: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "request": "ru.doczilla.workspace.table.Workspace",
            "action": "read",
            "section": section_id,
            "folderId": folder_id,
            "fields": fields,
            "sort": sort,
        }
        if filter_expr:
            params["filter"] = filter_expr
        data = await self._request(params)
        items = data.get("data", [])
        return items if isinstance(items, list) else []

    async def get_templates(self, section_id: str) -> list[dict]:
        """
        Получить список опубликованных шаблонов (dotx) из раздела,
        включая один уровень вложенных папок.
        """
        root_folder_id = "00000000-0000-0000-0000-000000000000"
        fields = '["name", "recordId", "link", "isFolder", "type"]'
        sort = '[{"property":"isFolder","direction":"desc"},{"property":"lastModified","direction":"desc"}]'

        # 1) Один запрос в корень без фильтра:
        #    получаем и шаблоны, и папки первого уровня.
        root_items = await self._read_workspace(
            section_id=section_id,
            folder_id=root_folder_id,
            fields=fields,
            sort=sort,
            filter_expr=None,
        )

        templates: list[dict] = []
        folders: list[dict] = []

        for item in root_items:
            if not isinstance(item, dict):
                continue
            if self._is_folder(item):
                folders.append(item)
                continue
            record_id = str(item.get("recordId") or "").strip()
            link = str(item.get("link") or "").strip()
            name = str(item.get("name") or "").strip()
            if not (record_id and link and name):
                continue
            templates.append({
                "name": name,
                "recordId": record_id,
                "link": link,
                "isFolder": False,
                "folderId": root_folder_id,
                "folderName": None,
            })

        # 2) Проходим по папкам первого уровня и читаем их содержимое.
        for folder in folders:
            if not isinstance(folder, dict):
                continue
            folder_id = str(folder.get("recordId") or "").strip()
            folder_name = str(folder.get("name") or "").strip() or None
            if not folder_id:
                continue

            child_items = await self._read_workspace(
                section_id=section_id,
                folder_id=folder_id,
                fields=fields,
                sort=sort,
                filter_expr=None,
            )
            for item in child_items:
                if not isinstance(item, dict):
                    continue
                if self._is_folder(item):
                    continue
                record_id = str(item.get("recordId") or "").strip()
                link = str(item.get("link") or "").strip()
                name = str(item.get("name") or "").strip()
                if not (record_id and link and name):
                    continue
                templates.append({
                    "name": name,
                    "recordId": record_id,
                    "link": link,
                    "isFolder": False,
                    "folderId": folder_id,
                    "folderName": folder_name,
                })

        # 3) Дедупликация по recordId.
        uniq: dict[str, dict] = {}
        for item in templates:
            rid = item["recordId"]
            if rid not in uniq:
                uniq[rid] = item
        result = list(uniq.values())
        logger.info(
            "Doczilla templates scan: section=%s root_items=%d folders=%d total=%d",
            section_id,
            len(root_items),
            len([f for f in folders if isinstance(f, dict)]),
            len(result),
        )
        return result

    async def get_document_info_by_id(self, file_id: str) -> dict:
        """Получить метаданные документа по его recordId. Метод 3.1.3."""
        data = await self._request({
            "request": "pro.doczilla.gpt.workspace.table.Workspace",
            "action": "content",
            "method": "getById",
            "file": file_id,
        })
        return data.get("data", {})

    # ── 3.2 Работа с документом ───────────────────────────────────────────────

    async def create_docz(
        self,
        template_file_id: str,
        template_link: str,
        name: str,
        folder_id: str = "",
    ) -> str:
        """
        Создать анкету (docz) из шаблона. Метод 3.2.1.
        Возвращает recordId созданной анкеты.
        """
        data = await self._request({
            "request": "pro.doczilla.gpt.workspace.table.Workspace",
            "method": "createDocz",
            "action": "content",
            "file": template_file_id,
            "link": template_link,
            "folder": folder_id,
            "name": name,
        })
        # API возвращает recordId созданного документа
        record = data.get("data", {})
        doc_id = record.get("recordId") or record.get("id")
        if not doc_id:
            raise DoczillaError([{"text": f"createDocz: не получен recordId. data={record}"}])
        return doc_id

    async def fill_docz(self, doc_id: str, variables: dict[str, Any]) -> None:
        """
        Заполнить переменные анкеты. Метод 3.2.2.
        variables: {"ИмяПеременной": "значение", ...}
        """
        import json
        await self._request({
            "request": "pro.doczilla.gpt.workspace.table.Workspace",
            "action": "content",
            "method": "fillDocz",
            "data": json.dumps(variables, ensure_ascii=False),
            "id": doc_id,
        })
        logger.debug("Doczilla fillDocz: заполнено %d переменных для %s", len(variables), doc_id)

    async def get_document_pdf(self, doc_id: str) -> bytes:
        """
        Скачать документ в формате PDF. Метод 3.2.3.
        Возвращает сырые байты PDF.
        """
        session = await get_session(self._client)
        response = await self._client.post(
            _ENDPOINT,
            data={
                "request": "pro.doczilla.gpt.workspace.table.Workspace",
                "action": "content",
                "method": "get",
                "file": doc_id,
                "contentType": "pdf",
                "session": session,
            },
        )
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        if "application/pdf" not in content_type and "octet-stream" not in content_type:
            # Если вернулся JSON — значит ошибка
            try:
                data = response.json()
                messages = data.get("info", {}).get("messages", [])
                raise DoczillaError(messages)
            except ValueError:
                pass  # не JSON — значит всё норм, это файл

        return response.content

    async def rename_document(self, doc_id: str, name: str) -> None:
        """Переименовать документ. Метод 3.2.4."""
        await self._request({
            "request": "pro.doczilla.gpt.workspace.table.Workspace",
            "method": "rename",
            "name": name,
            "action": "content",
            "file": doc_id,
        })

    # ── 4. Схема данных (переменные шаблонов) ─────────────────────────────────

    async def get_template_structure(self, file_id: str) -> dict:
        """
        Получить полную структуру переменных шаблона. Метод 4.3.
        Используется при первичной настройке маппинга.
        Returns: {"variables": [...], "sections": [...], ...}
        """
        data = await self._request({
            "request": "ru.doczilla.workspace.table.Workspace",
            "action": "content",
            "method": "structureRead",
            "type": "data",
            "file": file_id,
        })
        # Поддержка разных форматов ответа:
        # 1) {"data": {"scheme": ...}}
        # 2) {"structure": {"scheme": ...}}
        if isinstance(data.get("data"), dict) and data["data"]:
            return data["data"]
        if isinstance(data.get("structure"), dict) and data["structure"]:
            return data["structure"]
        return {}

    async def get_template_variables(self, file_id: str) -> dict:
        """
        Получить текущие значения переменных документа. Метод 4.1.
        Полезно для отладки и проверки заполнения.
        """
        data = await self._request({
            "request": "ru.doczilla.workspace.table.Workspace",
            "action": "content",
            "method": "structureRead",
            "type": "values",
            "file": file_id,
        })
        return data.get("data", {})
