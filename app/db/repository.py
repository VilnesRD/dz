"""
Репозиторий — CRUD-операции с БД.
Все методы синхронные (SQLite не нуждается в async).
"""
import json
from datetime import datetime
from typing import Optional
from sqlalchemy.orm import Session

from app.db.models import Template, FieldMapping, GenerationLog, User


# ── Users ─────────────────────────────────────────────────────────────────────

def get_user(db: Session, username: str) -> Optional[User]:
    return db.query(User).filter(User.username == username).first()

def create_user(db: Session, username: str, password_hash: str) -> User:
    user = User(username=username, password_hash=password_hash)
    db.add(user); db.commit(); db.refresh(user)
    return user

def user_count(db: Session) -> int:
    return db.query(User).count()


# ── Templates ─────────────────────────────────────────────────────────────────

def list_templates(db: Session) -> list[Template]:
    return db.query(Template).order_by(Template.created_at.desc()).all()

def get_template(db: Session, template_id: int) -> Optional[Template]:
    return db.query(Template).filter(Template.id == template_id).first()

def get_template_by_key(db: Session, key: str) -> Optional[Template]:
    return db.query(Template).filter(Template.key == key).first()

def create_template(db: Session, **kwargs) -> Template:
    t = Template(**kwargs)
    db.add(t); db.commit(); db.refresh(t)
    return t

def update_template(db: Session, template_id: int, **kwargs) -> Optional[Template]:
    t = get_template(db, template_id)
    if not t: return None
    for k, v in kwargs.items():
        setattr(t, k, v)
    db.commit(); db.refresh(t)
    return t

def delete_template(db: Session, template_id: int) -> bool:
    t = get_template(db, template_id)
    if not t: return False
    db.delete(t); db.commit()
    return True

def save_template_structure(db: Session, template_id: int, structure: dict) -> None:
    """Кэшировать ответ structureRead в поле structure_json."""
    update_template(db, template_id,
        structure_json=json.dumps(structure, ensure_ascii=False),
        structure_updated_at=datetime.utcnow())


# ── Field Mappings ────────────────────────────────────────────────────────────

def get_mappings(db: Session, template_id: int) -> list[FieldMapping]:
    return (db.query(FieldMapping)
              .filter(FieldMapping.template_id == template_id)
              .all())

def upsert_mapping(db: Session, template_id: int, variable_id: str, **kwargs) -> FieldMapping:
    """Создать или обновить маппинг переменной."""
    m = (db.query(FieldMapping)
           .filter(FieldMapping.template_id == template_id,
                   FieldMapping.variable_id == variable_id)
           .first())
    if m:
        for k, v in kwargs.items():
            setattr(m, k, v)
    else:
        m = FieldMapping(template_id=template_id, variable_id=variable_id, **kwargs)
        db.add(m)
    db.commit(); db.refresh(m)
    return m

def bulk_upsert_mappings(db: Session, template_id: int, mappings: list[dict]) -> None:
    """Массовое обновление маппингов шаблона."""
    for item in mappings:
        vid = item.pop("variable_id")
        upsert_mapping(db, template_id, vid, **item)

def delete_mappings(db: Session, template_id: int) -> None:
    db.query(FieldMapping).filter(FieldMapping.template_id == template_id).delete()
    db.commit()


# ── Generation Logs ───────────────────────────────────────────────────────────

def create_log(db: Session, **kwargs) -> GenerationLog:
    log = GenerationLog(**kwargs)
    db.add(log); db.commit(); db.refresh(log)
    return log

def update_log(db: Session, log_id: int, **kwargs) -> None:
    log = db.query(GenerationLog).filter(GenerationLog.id == log_id).first()
    if log:
        for k, v in kwargs.items():
            setattr(log, k, v)
        db.commit()

def list_logs(db: Session, limit: int = 100) -> list[GenerationLog]:
    return (db.query(GenerationLog)
              .order_by(GenerationLog.created_at.desc())
              .limit(limit)
              .all())
