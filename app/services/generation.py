"""
Сервис генерации документов.

Оркестрирует полный сценарий:
    Б24 (данные) → маппинг из БД → Doczilla (генерация) → Б24 (результат)

Шаги:
    1. Загрузить шаблон из SQLite по template_key
    2. Получить данные сделки, контакта, компании из Б24
    3. Построить payload для fillDocz (mapper_db.py)
    4. Doczilla: createDocz → fillDocz
    5. Сформировать ссылку на документ
    6. Записать ссылку в поле сделки Б24
    7. Добавить комментарий в ленту сделки
"""
import logging
from dataclasses import dataclass
import re
import json

from app.services.bitrix_client import BitrixClient
from app.services.doczilla_client import DoczillaClient
from app.services.mapper_db import build_fill_payload, build_doc_name
from app.core.config import get_settings
from app.db.database import SessionLocal
from app.db import repository as repo

logger = logging.getLogger(__name__)
settings = get_settings()


@dataclass
class GenerationResult:
    doc_id: str
    doc_link: str
    doc_name: str
    template_id: int
    result_mode: str
    save_link: bool
    save_pdf: bool
    warnings: list[str]


class DocumentGenerationService:

    def __init__(self, bitrix: BitrixClient, doczilla: DoczillaClient):
        self.bitrix = bitrix
        self.doczilla = doczilla

    @staticmethod
    def _normalize_result_mode(value: str | None) -> str:
        mode = str(value or "").strip().lower()
        return mode if mode in {"link", "pdf", "both"} else "both"

    @staticmethod
    def _count_non_empty_payload(payload: dict[str, object]) -> int:
        return sum(1 for v in payload.values() if v not in (None, "", [], {}, ()))

    @staticmethod
    def _value_preview(value: object, limit: int = 140) -> str:
        text = str(value)
        text = text.replace("\n", "\\n")
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    def _log_fill_payload(self, template, payload: dict[str, object], log_prefix: str) -> None:
        mappings = list(getattr(template, "mappings", []) or [])
        by_id = {str(m.variable_id): m for m in mappings}
        payload_keys = {str(k) for k in payload.keys()}

        logger.info("%s: fill payload details START (vars=%d)", log_prefix, len(payload))
        for var_id, value in payload.items():
            key = str(var_id)
            m = by_id.get(key)
            if m is None:
                logger.info(
                    "%s: fill var id=%s value=%s",
                    log_prefix, key, self._value_preview(value),
                )
                continue
            logger.info(
                "%s: fill var id=%s name='%s' kind=%s type=%s source=%s -> %s",
                log_prefix,
                key,
                str(m.variable_name or "").strip(),
                str(m.variable_kind or "").strip(),
                str(m.variable_type or "").strip(),
                str(m.source_type or "").strip(),
                self._value_preview(value),
            )

        skipped = []
        for m in mappings:
            if str(m.source_type or "").strip().lower() == "skip":
                continue
            mid = str(m.variable_id)
            if mid not in payload_keys:
                skipped.append(f"{mid}:{str(m.variable_name or '').strip()}")
        if skipped:
            preview = ", ".join(skipped[:30])
            if len(skipped) > 30:
                preview += f" ... (+{len(skipped) - 30})"
            logger.info("%s: skipped vars (%d): %s", log_prefix, len(skipped), preview)
        logger.info("%s: fill payload details END", log_prefix)

    @staticmethod
    def _humanize_source_path(path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return "источник не указан"
        if "." not in text:
            return text
        source, field = text.split(".", 1)
        source_labels = {
            "deal": "Сделка",
            "lead": "Лид",
            "contact": "Контакт",
            "company": "Компания",
            "doc": "Документ",
        }
        return f"{source_labels.get(source, source)}: {field}"

    def _build_empty_source_warnings(self, diagnostics: list[dict[str, str]]) -> list[str]:
        if not diagnostics:
            return []
        warnings: list[str] = []
        for item in diagnostics:
            source_type = str(item.get("source_type") or "").strip().lower()
            if source_type not in {"field", "formula", "selector_map"}:
                continue

            var_name = str(item.get("variable_name") or "").strip() or f"id={item.get('variable_id')}"
            source_value = str(item.get("source_value") or "").strip()
            source_title = source_value
            if source_type == "selector_map":
                # selector_map хранит JSON, показываем source_field из конфига.
                try:
                    cfg = json.loads(source_value) if source_value else {}
                except Exception:
                    cfg = {}
                source_title = str(cfg.get("source_field") or "").strip() or source_value

            if source_type == "formula":
                source_desc = "формула"
            else:
                source_desc = self._humanize_source_path(source_title)

            warnings.append(f"Пустой источник для «{var_name}»: {source_desc}")

        # Дедупликация + ограничение размера ответа.
        uniq: list[str] = []
        seen = set()
        for w in warnings:
            if w in seen:
                continue
            seen.add(w)
            uniq.append(w)
        limit = 10
        if len(uniq) > limit:
            extra = len(uniq) - limit
            return uniq[:limit] + [f"И ещё {extra} пустых полей по маппингу."]
        return uniq

    @staticmethod
    def _build_doc_link(doc_id: str, doc_link_code: str) -> str:
        base = settings.DOCZILLA_BASE_URL.rstrip("/")
        if doc_link_code:
            if doc_link_code.startswith("http://") or doc_link_code.startswith("https://"):
                return doc_link_code
            return f"{base}/#{doc_link_code}"
        return f"{base}/workspace#file/{doc_id}"

    @staticmethod
    def _load_template(template_key: str):
        with SessionLocal() as db:
            template = repo.get_template_by_key(db, template_key)
            if not template:
                raise KeyError(f"Шаблон '{template_key}' не найден в базе данных")
            if not template.active:
                raise ValueError(f"Шаблон '{template_key}' отключён")
            _ = template.mappings  # eager load
            return template

    async def _create_and_fill_doc(
        self,
        *,
        template,
        payload: dict[str, object],
        doc_name: str,
        log_prefix: str,
    ) -> tuple[str, str]:
        logger.info("%s: создаём документ в Doczilla", log_prefix)
        doc = await self.doczilla.create_docz(
            template_file_id=template.doczilla_file_id,
            template_link=template.doczilla_link,
            name=doc_name,
            folder_id=template.doczilla_folder_id,
        )
        doc_link_code = ""
        if isinstance(doc, dict):
            doc_id = str(doc.get("id") or doc.get("recordId") or "").strip()
            doc_link_code = str(doc.get("link") or "").strip()
        else:
            doc_id = str(doc).strip()
        if not doc_id:
            raise RuntimeError("Doczilla createDocz не вернул ID документа")
        logger.info("%s: doc_id=%s link_code=%s, заполняем переменные", log_prefix, doc_id, doc_link_code or "-")

        # Диагностика прав доступа в Doczilla: документ создаётся от service-аккаунта.
        # Это помогает понять, почему у конечного пользователя может быть "нет доступа".
        try:
            info = await self.doczilla.get_document_info_by_id(doc_id)
            if isinstance(info, dict):
                access = str(info.get("access") or "").strip()
                folder_id = str(info.get("folderId") or "").strip()
                owner_login = str(
                    info.get("author.login")
                    or info.get("user.login")
                    or ""
                ).strip()
                shared_to = str(info.get("sharedToEmailId") or "").strip()
                logger.info(
                    "%s: doc access snapshot id=%s owner=%s folder=%s access=%s sharedTo=%s",
                    log_prefix,
                    doc_id,
                    owner_login or "-",
                    folder_id or "-",
                    access or "-",
                    shared_to or "-",
                )
        except Exception as e:
            logger.warning("%s: не удалось получить access snapshot doc_id=%s: %s", log_prefix, doc_id, e)

        await self.doczilla.fill_docz(doc_id, payload)
        return doc_id, self._build_doc_link(doc_id, doc_link_code)

    async def generate_for_deal(self, deal_id: str, template_key: str) -> GenerationResult:
        """
        Полный цикл генерации документа для сделки.
        Шаблон и маппинг берутся из SQLite.
        """
        template = self._load_template(template_key)

        # ── 2. Данные из Б24 ──────────────────────────────────────────────────
        logger.info("deal=%s шаблон=%s: получаем данные из Б24", deal_id, template_key)
        deal = await self.bitrix.get_deal(deal_id)

        contact = None
        company = None

        contact_ids = deal.get("CONTACT_ID") or []
        if isinstance(contact_ids, str) and contact_ids:
            contact_ids = [contact_ids]
        if contact_ids:
            try:
                contact = await self.bitrix.get_contact(contact_ids[0])
            except Exception as e:
                logger.warning("deal=%s: не удалось загрузить контакт: %s", deal_id, e)

        company_id = deal.get("COMPANY_ID")
        if company_id:
            try:
                company = await self.bitrix.get_company(company_id)
            except Exception as e:
                logger.warning("deal=%s: не удалось загрузить компанию: %s", deal_id, e)

        # ── 3. Маппинг полей ──────────────────────────────────────────────────
        diagnostics: list[dict[str, str]] = []
        payload = build_fill_payload(template, deal, contact, company, lead=None, diagnostics=diagnostics)
        doc_name = build_doc_name(
            template,
            deal=deal,
            contact=contact,
            company=company,
            lead=None,
            entity_type="deal",
        )
        warnings: list[str] = self._build_empty_source_warnings(diagnostics)
        non_empty = self._count_non_empty_payload(payload)
        logger.info("deal=%s: payload %d переменных (непустых=%d)", deal_id, len(payload), non_empty)
        self._log_fill_payload(template, payload, f"deal={deal_id}")

        # ── 4. Создать и заполнить документ в Doczilla ────────────────────────
        doc_id, doc_link = await self._create_and_fill_doc(
            template=template,
            payload=payload,
            doc_name=doc_name,
            log_prefix=f"deal={deal_id}",
        )

        # ── 6. Записать результат в Б24 ───────────────────────────────────────
        result_mode = self._normalize_result_mode(getattr(template, "bitrix_result_mode", "both"))
        save_link = result_mode in {"link", "both"}
        save_pdf = result_mode in {"pdf", "both"}

        link_field = str(getattr(template, "bitrix_deal_link_field", "") or settings.BITRIX_DEAL_LINK_FIELD or "").strip()
        link_multiple = bool(getattr(template, "bitrix_deal_link_multiple", False))
        if save_link and link_field:
            try:
                logger.info(
                    "deal=%s: записываем ссылку в Б24 поле %s (multiple=%s)",
                    deal_id, link_field, link_multiple,
                )
                await self.bitrix.set_deal_field(deal_id, link_field, doc_link, multiple=link_multiple)
            except Exception as e:
                warn = f"Не удалось сохранить ссылку в поле {link_field}: {e}"
                warnings.append(warn)
                logger.warning("deal=%s: %s", deal_id, warn)

        pdf_field = str(getattr(template, "bitrix_deal_pdf_field", "") or "").strip()
        pdf_multiple = bool(getattr(template, "bitrix_deal_pdf_multiple", False))
        if save_pdf and pdf_field:
            try:
                logger.info(
                    "deal=%s: получаем PDF и загружаем в поле %s (multiple=%s)",
                    deal_id, pdf_field, pdf_multiple,
                )
                pdf_bytes = await self.doczilla.get_document_pdf(doc_id)
                filename = _make_pdf_filename(doc_name)
                await self.bitrix.set_deal_file_field(
                    deal_id,
                    pdf_field,
                    filename,
                    pdf_bytes,
                    multiple=pdf_multiple,
                )
            except Exception as e:
                warn = f"Не удалось сохранить PDF в поле {pdf_field}: {e}"
                warnings.append(warn)
                logger.warning("deal=%s: %s", deal_id, warn)

        # ── 7. Комментарий в ленту ────────────────────────────────────────────
        comment = f"✅ Документ сгенерирован: {doc_name}\n🔗 {doc_link}"
        if warnings:
            comment += "\n⚠️ " + "\n⚠️ ".join(warnings)
        try:
            await self.bitrix.add_deal_comment(deal_id, comment)
        except Exception as e:
            logger.warning("deal=%s: не удалось добавить комментарий: %s", deal_id, e)

        logger.info("deal=%s: ✅ готово, doc_id=%s", deal_id, doc_id)
        return GenerationResult(
            doc_id=doc_id,
            doc_link=doc_link,
            doc_name=doc_name,
            template_id=template.id,
            result_mode=result_mode,
            save_link=save_link,
            save_pdf=save_pdf,
            warnings=warnings,
        )

    async def generate_for_lead(self, lead_id: str, template_key: str) -> GenerationResult:
        """
        Генерация документа для лида.
        Источник маппинга: lead.*, contact.*, company.*, doc.*.
        """
        template = self._load_template(template_key)
        logger.info("lead=%s шаблон=%s: получаем данные из Б24", lead_id, template_key)
        lead = await self.bitrix.get_lead(lead_id)

        contact = None
        company = None

        contact_id = lead.get("CONTACT_ID")
        if contact_id:
            try:
                contact = await self.bitrix.get_contact(contact_id)
            except Exception as e:
                logger.warning("lead=%s: не удалось загрузить контакт: %s", lead_id, e)

        company_id = lead.get("COMPANY_ID")
        if company_id:
            try:
                company = await self.bitrix.get_company(company_id)
            except Exception as e:
                logger.warning("lead=%s: не удалось загрузить компанию: %s", lead_id, e)

        diagnostics: list[dict[str, str]] = []
        payload = build_fill_payload(template, deal={}, contact=contact, company=company, lead=lead, diagnostics=diagnostics)
        doc_name = build_doc_name(
            template,
            deal=None,
            contact=contact,
            company=company,
            lead=lead,
            entity_type="lead",
        )
        warnings: list[str] = self._build_empty_source_warnings(diagnostics)
        non_empty = self._count_non_empty_payload(payload)
        logger.info("lead=%s: payload %d переменных (непустых=%d)", lead_id, len(payload), non_empty)
        self._log_fill_payload(template, payload, f"lead={lead_id}")

        doc_id, doc_link = await self._create_and_fill_doc(
            template=template,
            payload=payload,
            doc_name=doc_name,
            log_prefix=f"lead={lead_id}",
        )

        # Для сценария USERFIELD_TYPE возвращаем результат в UI,
        # который сам выставляет значение свойства через BX24.placement.call('setValue').
        logger.info("lead=%s: ✅ готово, doc_id=%s", lead_id, doc_id)
        return GenerationResult(
            doc_id=doc_id,
            doc_link=doc_link,
            doc_name=doc_name,
            template_id=template.id,
            result_mode="link",
            save_link=False,
            save_pdf=False,
            warnings=warnings,
        )

    async def generate_for_contact(self, contact_id: str, template_key: str) -> GenerationResult:
        """
        Генерация документа для контакта.
        """
        template = self._load_template(template_key)
        logger.info("contact=%s шаблон=%s: получаем данные из Б24", contact_id, template_key)
        contact = await self.bitrix.get_contact(contact_id)

        company = None
        company_id = contact.get("COMPANY_ID")
        if company_id:
            try:
                company = await self.bitrix.get_company(company_id)
            except Exception as e:
                logger.warning("contact=%s: не удалось загрузить компанию: %s", contact_id, e)

        diagnostics: list[dict[str, str]] = []
        payload = build_fill_payload(template, deal={}, contact=contact, company=company, lead=None, diagnostics=diagnostics)
        doc_name = build_doc_name(
            template,
            deal=None,
            contact=contact,
            company=company,
            lead=None,
            entity_type="contact",
        )
        warnings: list[str] = self._build_empty_source_warnings(diagnostics)
        non_empty = self._count_non_empty_payload(payload)
        logger.info("contact=%s: payload %d переменных (непустых=%d)", contact_id, len(payload), non_empty)
        self._log_fill_payload(template, payload, f"contact={contact_id}")

        doc_id, doc_link = await self._create_and_fill_doc(
            template=template,
            payload=payload,
            doc_name=doc_name,
            log_prefix=f"contact={contact_id}",
        )
        logger.info("contact=%s: ✅ готово, doc_id=%s", contact_id, doc_id)
        return GenerationResult(
            doc_id=doc_id,
            doc_link=doc_link,
            doc_name=doc_name,
            template_id=template.id,
            result_mode="link",
            save_link=False,
            save_pdf=False,
            warnings=warnings,
        )

    async def generate_for_company(self, company_id: str, template_key: str) -> GenerationResult:
        """
        Генерация документа для компании.
        """
        template = self._load_template(template_key)
        logger.info("company=%s шаблон=%s: получаем данные из Б24", company_id, template_key)
        company = await self.bitrix.get_company(company_id)

        diagnostics: list[dict[str, str]] = []
        payload = build_fill_payload(template, deal={}, contact=None, company=company, lead=None, diagnostics=diagnostics)
        doc_name = build_doc_name(
            template,
            deal=None,
            contact=None,
            company=company,
            lead=None,
            entity_type="company",
        )
        warnings: list[str] = self._build_empty_source_warnings(diagnostics)
        non_empty = self._count_non_empty_payload(payload)
        logger.info("company=%s: payload %d переменных (непустых=%d)", company_id, len(payload), non_empty)
        self._log_fill_payload(template, payload, f"company={company_id}")

        doc_id, doc_link = await self._create_and_fill_doc(
            template=template,
            payload=payload,
            doc_name=doc_name,
            log_prefix=f"company={company_id}",
        )
        logger.info("company=%s: ✅ готово, doc_id=%s", company_id, doc_id)
        return GenerationResult(
            doc_id=doc_id,
            doc_link=doc_link,
            doc_name=doc_name,
            template_id=template.id,
            result_mode="link",
            save_link=False,
            save_pdf=False,
            warnings=warnings,
        )


def _make_pdf_filename(doc_name: str) -> str:
    name = (doc_name or "document").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "document"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name
