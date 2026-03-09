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

    # ── Битрикс24 ─────────────────────────────────────────────────────────────
    BITRIX_WEBHOOK_URL: str         # https://crm-test.doczilla.pro/rest/1/xxxxx/
    BITRIX_DEAL_LINK_FIELD: str = "UF_CRM_DOCZILLA_LINK"

    # ── Приложение ────────────────────────────────────────────────────────────
    APP_PUBLIC_URL: str = "https://bridge.vird.cloud"
    DEBUG: bool = False

    # ── База данных ───────────────────────────────────────────────────────────
    DB_PATH: str = "./data/db.sqlite3"

    # ── Админ-панель ──────────────────────────────────────────────────────────
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_SECRET_KEY: str = "change-me-in-production-min-32-chars!!"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
