"""
ORM-модели SQLite.

Схема:
  users            — учётные записи для админ-панели
  templates        — шаблоны Doczilla с кэшем structureRead
  field_mappings   — маппинг переменных шаблона ← поля Б24
  generation_logs  — лог каждой генерации документа
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, Boolean,
    DateTime, ForeignKey, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from app.db.database import Base


class User(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True)
    username   = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class Template(Base):
    """
    Шаблон Doczilla, зарегистрированный в системе.
    После сохранения — система автоматически вызывает structureRead
    и кэширует ответ в structure_json.
    """
    __tablename__ = "templates"

    id            = Column(Integer, primary_key=True)
    key           = Column(String(64), unique=True, nullable=False)  # ключ шаблона для /api/generate
    name          = Column(String(256), nullable=False)
    doczilla_file_id = Column(String(128), nullable=False)           # recordId
    doczilla_link    = Column(String(64),  nullable=False)           # короткий link
    doczilla_folder_id = Column(String(128), default="00000000-0000-0000-0000-000000000000")
    doc_name_template  = Column(String(256), default="Документ {deal_id}")
    bitrix_result_mode = Column(String(16), nullable=False, default="both")  # link | pdf | both
    bitrix_deal_link_field = Column(String(128), nullable=False, default="")  # UF_* поле сделки для ссылки
    bitrix_deal_link_multiple = Column(Boolean, nullable=False, default=False)  # множественное LINK поле
    bitrix_deal_pdf_field  = Column(String(128), nullable=False, default="")  # UF_* (file) поле сделки для PDF
    bitrix_deal_pdf_multiple = Column(Boolean, nullable=False, default=False)  # множественное FILE поле
    structure_json     = Column(Text, nullable=True)                 # кэш structureRead
    structure_updated_at = Column(DateTime, nullable=True)
    active        = Column(Boolean, default=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

    mappings = relationship("FieldMapping", back_populates="template",
                            cascade="all, delete-orphan")


class FieldMapping(Base):
    """
    Маппинг одной переменной шаблона Doczilla ← источник данных из Б24.

    variable_kind / variable_type берётся из structureRead и кэшируется здесь.

    source_type:
      "field"        — одно поле Б24: "deal.TITLE", "contact.NAME"
      "formula"      — шаблон из нескольких полей: "{deal.NAME} {contact.LAST_NAME}"
      "literal"      — фиксированное значение: "Москва"
      "selector_map" — JSON: {"source_field":"deal.X","options":{"val":"cond_id",...}}
      "skip"         — не заполнять (оставить как есть)

    parent_variable_id:
      Для переменных, вложенных в condition/replicator — ID родителя.
    """
    __tablename__ = "field_mappings"
    __table_args__ = (UniqueConstraint("template_id", "variable_id"),)

    id               = Column(Integer, primary_key=True)
    template_id      = Column(Integer, ForeignKey("templates.id", ondelete="CASCADE"), nullable=False)
    variable_id      = Column(String(32), nullable=False)    # числовой id из structureRead
    variable_name    = Column(String(256), nullable=False)   # human-readable имя
    variable_kind    = Column(String(32), nullable=False)    # variable/condition/selector/replicator
    variable_type    = Column(String(32), nullable=True)     # string/number/date/boolean/money/percent/link
    source_type      = Column(String(32), nullable=False, default="skip")
    source_value     = Column(Text, nullable=False, default="")
    parent_variable_id = Column(String(32), nullable=True)  # id родителя (для вложенных)

    template = relationship("Template", back_populates="mappings")


class GenerationLog(Base):
    """Лог каждой генерации документа."""
    __tablename__ = "generation_logs"

    id            = Column(Integer, primary_key=True)
    deal_id       = Column(String(32), nullable=True)
    template_id   = Column(Integer, nullable=True)
    template_key  = Column(String(64), nullable=True)
    doc_id        = Column(String(128), nullable=True)
    doc_link      = Column(Text, nullable=True)
    doc_name      = Column(String(256), nullable=True)
    status        = Column(String(16), nullable=False, default="pending")  # success/error
    error_message = Column(Text, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)

class OAuthToken(Base):
    """OAuth-токены Битрикс24 — сохраняются при установке приложения."""
    __tablename__ = "oauth_tokens"

    id            = Column(Integer, primary_key=True)
    domain        = Column(String(256), unique=True, nullable=False)
    access_token  = Column(String(512), nullable=False)
    refresh_token = Column(String(512), nullable=False)
    expires_at    = Column(DateTime, nullable=False)
    member_id     = Column(String(128), nullable=True)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
