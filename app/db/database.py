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
