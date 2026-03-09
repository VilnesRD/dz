"""
Менеджер сессии Doczilla PRO.

Doczilla использует сессионную аутентификацию: метод signin возвращает токен,
который нужно передавать в каждый запрос. Токен живёт ~30 минут.

Этот модуль кэширует токен и автоматически обновляет его при истечении
или при получении ошибки аутентификации (success=false).
"""
import time
import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class _SessionCache:
    token: str = ""
    expires_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def is_valid(self) -> bool:
        return bool(self.token) and time.monotonic() < self.expires_at


_cache = _SessionCache()


async def get_session(client: httpx.AsyncClient) -> str:
    """
    Возвращает действующий токен сессии.
    Если кэш пуст или истёк — выполняет signin автоматически.
    Lock защищает от параллельных повторных авторизаций.
    """
    if _cache.is_valid():
        return _cache.token

    async with _cache.lock:
        # Двойная проверка: пока ждали lock, другой поток мог обновить токен
        if _cache.is_valid():
            return _cache.token

        await _signin(client)
        return _cache.token


async def invalidate_session() -> None:
    """Сбросить кэш вручную (например, после ошибки 'session expired')."""
    _cache.token = ""
    _cache.expires_at = 0.0


async def _signin(client: httpx.AsyncClient) -> None:
    """Выполнить авторизацию и сохранить токен в кэш."""
    logger.info("Doczilla: выполняем signin...")
    response = await client.post(
        f"{settings.DOCZILLA_BASE_URL}/request.json",
        data={
            "request": "signin",
            "login": settings.DOCZILLA_LOGIN,
            "password": settings.DOCZILLA_PASSWORD,
        },
    )
    response.raise_for_status()
    data = response.json()

    if not data.get("success"):
        messages = data.get("info", {}).get("messages", [])
        raise RuntimeError(f"Doczilla signin failed: {messages}")

    token = data.get("session") or data.get("data", {}).get("session")
    if not token:
        raise RuntimeError(f"Doczilla signin: не получен токен. Ответ: {data}")

    _cache.token = token
    _cache.expires_at = time.monotonic() + settings.DOCZILLA_SESSION_TTL
    logger.info("Doczilla: сессия получена, TTL=%ds", settings.DOCZILLA_SESSION_TTL)
