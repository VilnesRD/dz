"""
FastAPI — точка входа.

Эндпоинты:
    GET  /health              — проверка работоспособности
    GET  /admin               — админ-панель (SPA)
    GET  /install             — страница установки Б24-приложения
    GET  /bitrix/widget       — виджет для iframe в карточке сделки
    GET  /api/widget-config   — список шаблонов для виджета
    POST /api/deal-info       — краткая информация о сделке (ID/название)
    POST /api/generate        — запуск генерации из iframe Б24
    /admin/*                  — REST API админ-панели
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
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

_doczilla_client: DoczillaClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _doczilla_client

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
    _doczilla_client = DoczillaClient()

    yield

    logger.info("Остановка...")
    if _doczilla_client:
        await _doczilla_client.signout()
        await _doczilla_client.close()


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


def _build_generation_service(
    bitrix_domain: str | None = None,
    bitrix_token: str | None = None,
) -> tuple[DocumentGenerationService, bool]:
    """
    Вернуть сервис генерации и флаг, нужно ли закрыть BitrixClient после запроса.
    """
    if _doczilla_client is None:
        raise RuntimeError("DoczillaClient не инициализирован")

    # Если domain не пришёл из iframe — пробуем последний портал из БД токенов.
    if not bitrix_domain:
        from app.db.database import SessionLocal
        from app.db import repository as repo
        with SessionLocal() as db:
            token = repo.get_latest_oauth_token(db)
        bitrix_domain = token.domain if token else None

    if not bitrix_domain:
        raise RuntimeError("Не указан bitrix_domain и в БД нет OAuth-токенов портала")

    bitrix = BitrixClient(domain=bitrix_domain, access_token=bitrix_token)
    return DocumentGenerationService(bitrix, _doczilla_client), True


# ── Роуты ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "url": settings.APP_PUBLIC_URL}


@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
@app.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
async def admin_panel():
    return FileResponse(STATIC_DIR / "admin.html")


# ── Ручной запуск для тестов ──────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    deal_id: str
    template_key: str = "contract"
    bitrix_domain: str | None = None
    bitrix_token: str | None = None


class DealInfoRequest(BaseModel):
    deal_id: str
    bitrix_domain: str | None = None
    bitrix_token: str | None = None


@app.post("/api/deal-info")
async def deal_info(req: DealInfoRequest):
    """Получить ID и название сделки для UI виджета."""
    client: BitrixClient | None = None
    try:
        if req.bitrix_domain:
            domain = req.bitrix_domain
        else:
            from app.db.database import SessionLocal
            from app.db import repository as repo
            with SessionLocal() as db:
                token = repo.get_latest_oauth_token(db)
            domain = token.domain if token else None

        if not domain:
            raise HTTPException(400, "Не указан domain портала Б24")

        client = BitrixClient(domain=domain, access_token=req.bitrix_token)
        deal = await client.get_deal(req.deal_id)
        deal_id = str(deal.get("ID") or req.deal_id)
        title = str(deal.get("TITLE") or f"Сделка #{deal_id}")
        return {"id": deal_id, "title": title}
    except BitrixError as e:
        raise HTTPException(502, str(e))
    finally:
        if client:
            await client.close()


@app.post("/api/generate")
async def manual_generate(req: GenerateRequest):
    """Генерация документа для сделки через OAuth local app."""
    svc: DocumentGenerationService | None = None
    owns_bitrix_client = False
    try:
        svc, owns_bitrix_client = _build_generation_service(req.bitrix_domain, req.bitrix_token)
        result = await svc.generate_for_deal(req.deal_id, req.template_key)
        return {
            "status": "success",
            "doc_id": result.doc_id,
            "doc_link": result.doc_link,
            "doc_name": result.doc_name,
            "warnings": result.warnings,
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
