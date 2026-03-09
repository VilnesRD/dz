"""
Настройка базы данных SQLite через SQLAlchemy (sync, для простоты).
БД создаётся автоматически при первом запуске.
"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DB_PATH = os.environ.get("DB_PATH", "./data/db.sqlite3")
os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

# Включить WAL и foreign keys для SQLite
@event.listens_for(engine, "connect")
def set_sqlite_pragma(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency — сессия БД на запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Создать все таблицы если не существуют. Вызывается при старте."""
    from app.db import models  # noqa: F401 — регистрирует модели
    Base.metadata.create_all(bind=engine)
    _migrate_templates_columns()


def _migrate_templates_columns():
    """
    Лёгкие миграции SQLite без Alembic:
    добавляем новые колонки в templates, если их ещё нет.
    """
    required = {
        "bitrix_result_mode": "ALTER TABLE templates ADD COLUMN bitrix_result_mode VARCHAR(16) NOT NULL DEFAULT 'both'",
        "bitrix_deal_link_field": "ALTER TABLE templates ADD COLUMN bitrix_deal_link_field VARCHAR(128) NOT NULL DEFAULT ''",
        "bitrix_deal_link_multiple": "ALTER TABLE templates ADD COLUMN bitrix_deal_link_multiple BOOLEAN NOT NULL DEFAULT 0",
        "bitrix_deal_pdf_field": "ALTER TABLE templates ADD COLUMN bitrix_deal_pdf_field VARCHAR(128) NOT NULL DEFAULT ''",
        "bitrix_deal_pdf_multiple": "ALTER TABLE templates ADD COLUMN bitrix_deal_pdf_multiple BOOLEAN NOT NULL DEFAULT 0",
    }
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(templates)").fetchall()
        existing = {str(row[1]) for row in rows if len(row) > 1}
        for column, ddl in required.items():
            if column in existing:
                continue
            conn.exec_driver_sql(ddl)
