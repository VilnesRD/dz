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
  selector_map — JSON-правила для condition (options/on_not_empty/on_empty)
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
    lead: dict | None = None,
) -> dict[str, Any]:
    """
    Построить payload для fillDocz на основе маппингов из БД.

    Returns:
        {"variable_id": value, ...}  — передаётся как data в fillDocz
    """
    sources = {
        "deal":    deal or {},
        "lead":    lead or {},
        "contact": contact or {},
        "company": company or {},
        "doc":     {},  # уже вычисленные переменные Doczilla (по variable_id)
    }

    payload: dict[str, Any] = {}

    # 1) Сначала считаем обычные поля/формулы/литералы.
    # Это позволяет в selector_map ссылаться на doc.<variable_id> из уже заполненных значений.
    regular = [m for m in template.mappings if m.source_type not in ("skip", "selector_map")]
    selector_maps = [m for m in template.mappings if m.source_type == "selector_map"]

    for group in (regular, selector_maps):
        for m in group:
            try:
                value = _resolve(m, sources)
                if value is not None:
                    payload[m.variable_id] = value
                    sources["doc"][m.variable_id] = value
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


def _raw_value(source_type: str, source_value: str, sources: dict) -> Any:
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
    raw = _get_field_raw(path, sources)
    return _stringify_value(raw)


def _get_field_raw(path: str, sources: dict) -> Any:
    """deal.TITLE → raw value из источника (без приведения к строке)."""
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

    return obj.get(field, "")


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

    field_raw = _get_field_raw(cfg.get("source_field", ""), sources)
    field_tokens = _tokenize_values(field_raw)
    this_cid = str(cfg.get("this_condition_id", m.variable_id))

    matched_ids: set[str] = set()

    # v2: rules = [{"operator":"eq|neq|in|not_in|contains|not_contains|filled|empty","value":"...","values":[...],"targets":[...]}]
    rules = cfg.get("rules")
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            targets = _extract_condition_ids(
                rule.get("targets")
                or rule.get("condition_ids")
                or rule.get("condition_id")
                or rule.get("set_true")
                or rule.get("true_ids")
            )
            if not targets:
                continue
            if _selector_rule_matches(rule, field_raw, field_tokens):
                matched_ids.update(targets)

    # v1 fallback: options map
    options = cfg.get("options", {})  # {field_value: condition_id|[condition_id,...]}
    if isinstance(options, dict):
        for expected, target in options.items():
            expected_tokens = _tokenize_values(expected)
            if expected_tokens and expected_tokens.intersection(field_tokens):
                matched_ids.update(_extract_condition_ids(target))
            elif str(expected).strip() in ("*", "__any__", "__non_empty__") and _is_non_empty(field_raw):
                matched_ids.update(_extract_condition_ids(target))

    # Дополнительные короткие правила:
    # on_not_empty / on_empty могут быть строкой id или списком id.
    if _is_non_empty(field_raw):
        matched_ids.update(_extract_condition_ids(cfg.get("on_not_empty")))
    else:
        matched_ids.update(_extract_condition_ids(cfg.get("on_empty")))

    return this_cid in matched_ids


def _coerce(raw: Any, typ: str | None, kind: str) -> Any:
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


def _stringify_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if "VALUE" in item and item.get("VALUE") is not None:
                    out.append(str(item.get("VALUE")))
                elif "ID" in item and item.get("ID") is not None:
                    out.append(str(item.get("ID")))
            elif item is not None:
                out.append(str(item))
        return ", ".join([x for x in out if x.strip()])
    if isinstance(value, dict):
        if value.get("VALUE") is not None:
            return str(value.get("VALUE"))
        if value.get("ID") is not None:
            return str(value.get("ID"))
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _tokenize_values(value: Any) -> set[str]:
    """
    Нормализовать значение (в т.ч. списки Б24) к набору токенов для сравнения.
    """
    tokens: set[str] = set()

    def push(v: Any):
        if v is None:
            return
        if isinstance(v, bool):
            tokens.add("true" if v else "false")
            return
        text = str(v).strip()
        if not text:
            return
        # Поддержка разделителей в строковых мультизначениях.
        parts = [p.strip() for p in re.split(r"[;,|]", text)]
        for part in parts:
            if part:
                tokens.add(part.lower())

    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                if "VALUE" in item:
                    push(item.get("VALUE"))
                elif "ID" in item:
                    push(item.get("ID"))
            else:
                push(item)
        return tokens

    if isinstance(value, dict):
        if "VALUE" in value:
            push(value.get("VALUE"))
        elif "ID" in value:
            push(value.get("ID"))
        return tokens

    push(value)
    return tokens


def _extract_condition_ids(value: Any) -> set[str]:
    ids: set[str] = set()
    if value is None:
        return ids
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        if text:
            ids.add(text)
        return ids
    if isinstance(value, list):
        for item in value:
            ids.update(_extract_condition_ids(item))
        return ids
    if isinstance(value, dict):
        for key in ("condition_id", "condition_ids", "set_true", "true_ids", "ids"):
            if key in value:
                ids.update(_extract_condition_ids(value.get(key)))
        return ids
    return ids


def _selector_rule_matches(rule: dict[str, Any], field_raw: Any, field_tokens: set[str]) -> bool:
    op = str(rule.get("operator") or rule.get("op") or "eq").strip().lower()

    if op in {"filled", "not_empty"}:
        return _is_non_empty(field_raw)
    if op in {"empty", "is_empty"}:
        return not _is_non_empty(field_raw)

    raw_values = rule.get("values")
    if raw_values is None:
        raw_values = rule.get("value")
    expected_tokens = _tokenize_values(raw_values)

    if op in {"eq", "="}:
        return bool(expected_tokens) and bool(expected_tokens.intersection(field_tokens))
    if op in {"neq", "!=", "<>"}:
        return bool(expected_tokens) and not bool(expected_tokens.intersection(field_tokens))
    if op == "in":
        return bool(expected_tokens) and bool(expected_tokens.intersection(field_tokens))
    if op == "not_in":
        return bool(expected_tokens) and not bool(expected_tokens.intersection(field_tokens))

    raw_text = _stringify_value(field_raw).lower()
    needle = _stringify_value(rule.get("value")).strip().lower()
    if op == "contains":
        return bool(needle) and needle in raw_text
    if op == "not_contains":
        return bool(needle) and needle not in raw_text

    # Неизвестный оператор трактуем как eq для обратной совместимости
    return bool(expected_tokens) and bool(expected_tokens.intersection(field_tokens))


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    return True
