"""
FastAPI — точка входа.

Эндпоинты:
    GET  /health              — проверка работоспособности
    GET  /admin               — админ-панель (SPA)
    GET  /install             — страница установки Б24-приложения
    GET  /bitrix/widget       — виджет для iframe в карточке сделки
    GET  /api/widget-config   — список шаблонов для виджета
    POST /webhook/bitrix      — вебхук от Б24 (кнопка в CRM)
    POST /api/generate        — ручной запуск генерации (для тестов)
    /admin/*                  — REST API админ-панели
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.services.bitrix_client import BitrixClient, BitrixError
from app.services.doczilla_client import DoczillaClient, DoczillaError
from app.services.generation import DocumentGenerationService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)
settings = get_settings()

STATIC_DIR = Path(__file__).parent / "static"

_bitrix_client: BitrixClient | None = None
_doczilla_client: DoczillaClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bitrix_client, _doczilla_client

    # Инициализация БД — создаёт таблицы если нет
    from app.db.database import init_db, SessionLocal
    from app.db import repository as repo
    from app.api.admin_api import pwd_ctx

    init_db()

    # Создать admin при первом старте
    with SessionLocal() as db:
        if repo.user_count(db) == 0:
            repo.create_user(db, settings.ADMIN_USERNAME, pwd_ctx.hash(settings.ADMIN_PASSWORD))
            logger.info("Создан пользователь '%s'", settings.ADMIN_USERNAME)

    logger.info("Запуск, APP_PUBLIC_URL=%s", settings.APP_PUBLIC_URL)
    _bitrix_client   = BitrixClient()
    _doczilla_client = DoczillaClient()

    yield

    logger.info("Остановка...")
    if _doczilla_client:
        await _doczilla_client.signout()
        await _doczilla_client.close()
    if _bitrix_client:
        await _bitrix_client.close()


app = FastAPI(
    title="Битрикс24 ↔ Doczilla PRO",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url=None,
)

from app.api.admin_api import router as admin_router
from app.api.widget    import router as widget_router

app.include_router(admin_router)
app.include_router(widget_router)


def _pick(*values):
    for value in values:
        if value:
            return str(value)
    return None


def _build_generation_service(bitrix_domain: str | None = None) -> tuple[DocumentGenerationService, bool]:
    """
    Вернуть сервис генерации и флаг, нужно ли закрыть BitrixClient после запроса.
    """
    if _doczilla_client is None:
        raise RuntimeError("DoczillaClient не инициализирован")

    # Для локального приложения важен domain портала: только так можно достать OAuth токены.
    if bitrix_domain:
        bitrix = BitrixClient(domain=bitrix_domain)
        return DocumentGenerationService(bitrix, _doczilla_client), True

    if _bitrix_client is None:
        raise RuntimeError("BitrixClient не инициализирован")
    return DocumentGenerationService(_bitrix_client, _doczilla_client), False


# ── Роуты ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "url": settings.APP_PUBLIC_URL}


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel():
    return FileResponse(STATIC_DIR / "admin.html")

@app.get("/webhook/bitrix")
async def bitrix_webhook_get():
    """GET-пинг от Б24 при установке приложения."""
    return {"status": "ok"}

@app.post("/webhook/bitrix")
async def bitrix_webhook(
    request: Request,
    bg: BackgroundTasks,
):
    """
    Вебхук от Б24 при нажатии кнопки в карточке сделки.
    Генерация запускается в фоне — Б24 получает 200 немедленно.
    """
    form = dict(await request.form())
    logger.info("Вебхук Б24: %s", {k: v for k, v in form.items() if "auth" not in k.lower()})

    deal_id = (
        form.get("data[FIELDS][ID]")
        or form.get("DEAL_ID")
        or form.get("deal_id")
    )
    template_key = (
        form.get("template_key")
        or form.get("TEMPLATE_KEY")
        or "contract"
    )
    bitrix_domain = _pick(
        form.get("DOMAIN"),
        form.get("domain"),
        form.get("auth[domain]"),
    )

    if not deal_id:
        raise HTTPException(400, "Отсутствует deal_id")

    bg.add_task(_run_generation, str(deal_id), template_key, bitrix_domain)
    return {
        "status": "accepted",
        "deal_id": deal_id,
        "template": template_key,
        "domain": bitrix_domain,
    }


async def _run_generation(deal_id: str, template_key: str, bitrix_domain: str | None = None):
    from app.db.database import SessionLocal
    from app.db import repository as repo

    with SessionLocal() as db:
        log_entry = repo.create_log(db, deal_id=deal_id, template_key=template_key, status="pending")

    svc: DocumentGenerationService | None = None
    owns_bitrix_client = False
    try:
        svc, owns_bitrix_client = _build_generation_service(bitrix_domain)
        result = await svc.generate_for_deal(deal_id, template_key)
        with SessionLocal() as db:
            repo.update_log(db, log_entry.id,
                status="success",
                doc_id=result.doc_id,
                doc_link=result.doc_link,
                doc_name=result.doc_name,
                template_id=result.template_id,
            )
        logger.info("✅ deal=%s doc_id=%s", deal_id, result.doc_id)
    except Exception as e:
        with SessionLocal() as db:
            repo.update_log(db, log_entry.id, status="error", error_message=str(e))
        logger.error("❌ deal=%s: %s", deal_id, e)
    finally:
        if owns_bitrix_client and svc:
            await svc.bitrix.close()


# ── Ручной запуск для тестов ──────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    deal_id: str
    template_key: str = "contract"
    bitrix_domain: str | None = None
    bitrix_token: str | None = None  # может быть передан из iframe Б24, но не обязателен


@app.post("/api/generate")
async def manual_generate(req: GenerateRequest):
    """Ручной запуск — для тестирования без Б24."""
    svc: DocumentGenerationService | None = None
    owns_bitrix_client = False
    try:
        svc, owns_bitrix_client = _build_generation_service(req.bitrix_domain)
        result = await svc.generate_for_deal(req.deal_id, req.template_key)
        return {
            "status": "success",
            "doc_id": result.doc_id,
            "doc_link": result.doc_link,
            "doc_name": result.doc_name,
        }
    except KeyError as e:
        raise HTTPException(404, str(e))
    except (BitrixError, DoczillaError) as e:
        raise HTTPException(502, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    finally:
        if owns_bitrix_client and svc:
            await svc.bitrix.close()
