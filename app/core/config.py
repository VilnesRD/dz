"""
Конфигурация приложения через переменные окружения (.env).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Doczilla PRO ──────────────────────────────────────────────────────────
    DOCZILLA_BASE_URL: str          # https://team.doczilla.pro
    DOCZILLA_LOGIN: str             # email
    DOCZILLA_PASSWORD: str          # пароль (plain, клиент сам хэширует если нужно)
    DOCZILLA_SESSION_TTL: int = 1800  # секунд (30 минут)
    DOCZILLA_TEMPLATES_SECTION_ID: str = "039BC112-E801-4F82-BA21-484F72500736"  # раздел с опубликованными dotx

    # ── Битрикс24 (локальное OAuth-приложение) ───────────────────────────────
    BITRIX_DEAL_LINK_FIELD: str = "UF_CRM_DOCZILLA_LINK"  # fallback, если не задано на уровне шаблона

    # ── Приложение ────────────────────────────────────────────────────────────
    APP_PUBLIC_URL: str = "https://bridge.vird.cloud"
    DEBUG: bool = False

    # ── База данных ───────────────────────────────────────────────────────────
    DB_PATH: str = "./data/db.sqlite3"

    # ── Админ-панель ──────────────────────────────────────────────────────────
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_SECRET_KEY: str = "change-me-in-production-min-32-chars!!"
    # Нужны для refresh_token. Для коротких операций можно жить на AUTH_ID.
    BITRIX_CLIENT_ID: str = ""
    BITRIX_CLIENT_SECRET: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
