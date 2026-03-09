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
    warnings: list[str]


class DocumentGenerationService:

    def __init__(self, bitrix: BitrixClient, doczilla: DoczillaClient):
        self.bitrix = bitrix
        self.doczilla = doczilla

    async def generate_for_deal(self, deal_id: str, template_key: str) -> GenerationResult:
        """
        Полный цикл генерации документа для сделки.
        Шаблон и маппинг берутся из SQLite.
        """
        # ── 1. Загрузить шаблон из БД ─────────────────────────────────────────
        with SessionLocal() as db:
            template = repo.get_template_by_key(db, template_key)
            if not template:
                raise KeyError(f"Шаблон '{template_key}' не найден в базе данных")
            if not template.active:
                raise ValueError(f"Шаблон '{template_key}' отключён")
            # Загружаем маппинги пока сессия открыта
            _ = template.mappings  # eager load

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
        payload = build_fill_payload(template, deal, contact, company)
        doc_name = build_doc_name(template, deal)
        warnings: list[str] = []
        non_empty = sum(
            1 for v in payload.values()
            if v not in (None, "", [], {}, ())
        )
        logger.info("deal=%s: payload %d переменных (непустых=%d)", deal_id, len(payload), non_empty)

        # ── 4. Создать и заполнить документ в Doczilla ────────────────────────
        logger.info("deal=%s: создаём документ в Doczilla", deal_id)
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
        logger.info("deal=%s: doc_id=%s link_code=%s, заполняем переменные", deal_id, doc_id, doc_link_code or "-")

        await self.doczilla.fill_docz(doc_id, payload)

        # ── 5. Ссылка на документ ─────────────────────────────────────────────
        base = settings.DOCZILLA_BASE_URL.rstrip("/")
        if doc_link_code:
            if doc_link_code.startswith("http://") or doc_link_code.startswith("https://"):
                doc_link = doc_link_code
            else:
                doc_link = f"{base}/#{doc_link_code}"
        else:
            doc_link = f"{base}/workspace#file/{doc_id}"

        # ── 6. Записать результат в Б24 ───────────────────────────────────────
        result_mode = str(getattr(template, "bitrix_result_mode", "") or "both").strip().lower()
        if result_mode not in {"link", "pdf", "both"}:
            result_mode = "both"
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
