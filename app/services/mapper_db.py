"""
Маппер на основе БД.

Читает FieldMapping из SQLite и строит payload для fillDocz:
    {str(variable_id): value, ...}

Поддерживаемые виды элементов Doczilla:
  variable   — простое значение (string/number/date/money/percent/link)
  condition  — boolean
  selector   — radio (один активный) или check (несколько)
  replicator — повторяющиеся строки (базовая поддержка, v1)

Поддерживаемые source_type:
  field        — одно поле Б24: "deal.TITLE"
  formula      — шаблон: "{deal.NAME} {contact.LAST_NAME}"
  literal      — фикс. значение: "ООО Ромашка"
  selector_map — JSON: {"source_field":"deal.X","options":{"Москва":"cond_id"}}
  skip         — пропустить
"""
from __future__ import annotations
import json
import logging
import re
from datetime import datetime
from typing import Any

from app.db.models import Template, FieldMapping

log = logging.getLogger(__name__)

# ── Публичный API ─────────────────────────────────────────────────────────────

def build_fill_payload(
    template: Template,
    deal: dict,
    contact: dict | None,
    company: dict | None,
) -> dict[str, Any]:
    """
    Построить payload для fillDocz на основе маппингов из БД.

    Returns:
        {"variable_id": value, ...}  — передаётся как data в fillDocz
    """
    sources = {
        "deal":    deal or {},
        "contact": contact or {},
        "company": company or {},
    }

    payload: dict[str, Any] = {}

    for m in template.mappings:
        if m.source_type == "skip":
            continue
        try:
            value = _resolve(m, sources)
            if value is not None:
                payload[m.variable_id] = value
        except Exception as e:
            log.warning("Маппинг id=%s ('%s'): %s", m.variable_id, m.variable_name, e)
            # Не прерываем генерацию из-за одного поля

    log.debug("fillDocz payload: %d элементов", len(payload))
    return payload


def build_doc_name(template: Template, deal: dict) -> str:
    """Сформировать имя файла по шаблону строки doc_name_template."""
    try:
        return template.doc_name_template.format(
            deal_id=deal.get("ID", ""),
            company=deal.get("COMPANY_TITLE", ""),
            date=datetime.now().strftime("%d.%m.%Y"),
        )
    except Exception:
        return f"Документ {deal.get('ID','')}"


# ── Резолверы ─────────────────────────────────────────────────────────────────

def _resolve(m: FieldMapping, sources: dict) -> Any:
    kind = m.variable_kind
    src  = m.source_type
    val  = m.source_value

    # selector_map обрабатываем отдельно — возвращает bool
    if src == "selector_map":
        return _resolve_selector(m, sources)

    # Получаем сырое значение
    raw = _raw_value(src, val, sources)

    # Приводим к нужному типу
    return _coerce(raw, m.variable_type, kind)


def _raw_value(source_type: str, source_value: str, sources: dict) -> str:
    if source_type == "literal":
        return source_value

    if source_type == "field":
        return _get_field(source_value, sources)

    if source_type == "formula":
        # Заменяем {deal.FIELD} → значение
        def replacer(match):
            path = match.group(1).strip()
            return _get_field(path, sources)
        return re.sub(r"\{([^}]+)\}", replacer, source_value)

    return ""


def _get_field(path: str, sources: dict) -> str:
    """deal.TITLE → sources['deal']['TITLE']"""
    if "." not in path:
        return ""
    source_name, field = path.split(".", 1)
    obj = sources.get(source_name, {})
    if not obj:
        return ""

    # Специальные поля
    if field in ("PHONE", "EMAIL"):
        items = obj.get(field, [])
        return items[0].get("VALUE", "") if isinstance(items, list) and items else ""

    if field == "ASSIGNED_BY_NAME":
        parts = [obj.get("ASSIGNED_BY_LAST_NAME",""),
                 obj.get("ASSIGNED_BY_NAME",""),
                 obj.get("ASSIGNED_BY_SECOND_NAME","")]
        return " ".join(p for p in parts if p).strip()

    val = obj.get(field, "")
    return "" if val is None else str(val)


def _resolve_selector(m: FieldMapping, sources: dict) -> bool | None:
    """
    selector_map: source_value = JSON
    {
      "source_field": "deal.UF_CRM_TARIFF",
      "options": {
        "pro":        "99",   // если поле == "pro"  → condition_id 99 = true
        "enterprise": "100"
      },
      "this_condition_id": "99"   // id текущего condition (этой строки маппинга)
    }
    Возвращает true/false для конкретного condition в зависимости от значения поля.
    """
    try:
        cfg = json.loads(m.source_value)
    except Exception:
        return None

    field_val = _get_field(cfg.get("source_field", ""), sources)
    options   = cfg.get("options", {})             # {field_value: condition_id}
    this_cid  = str(cfg.get("this_condition_id", m.variable_id))

    # Найти condition_id, которому соответствует текущее значение поля
    matched_cid = str(options.get(field_val, ""))
    return matched_cid == this_cid


def _coerce(raw: str, typ: str | None, kind: str) -> Any:
    """Привести строку к нужному типу Doczilla."""
    if kind == "condition":
        if isinstance(raw, bool):
            return raw
        return str(raw).lower() in ("1", "true", "yes", "да")

    if not raw and raw != 0:
        return None

    if typ in ("number", "money", "percent"):
        try:
            return float(raw) if "." in str(raw) else int(raw)
        except (ValueError, TypeError):
            return raw

    if typ == "date":
        # Пробуем привести ISO-дату Б24 к timestamp (ms) для Doczilla
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return raw

    # string, link, default
    return str(raw)
