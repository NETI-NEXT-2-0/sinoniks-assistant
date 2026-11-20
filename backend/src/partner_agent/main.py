import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from anyio import to_thread
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from partner_agent.accounting_routes import router as accounting_router
from partner_agent.auth.admin import router as admin_router
from partner_agent.auth.groups import router as groups_router
from partner_agent.auth.routes import router
from partner_agent.chats.files import router as chat_files_router
from partner_agent.chats.routes import router as chats_router
from partner_agent.design_routes import router as design_router
from partner_agent.documents.routes import router as documents_router
from partner_agent.infra.db import get_engine
from partner_agent.infra.errors import APIError
from partner_agent.infra.settings import get_settings
from partner_agent.infra.storage import Storage, create_s3_client
from partner_agent.runs.routes import router as runs_router
from partner_agent.vendor_wish_routes import router as vendor_wish_router
from partner_agent.web.routes import router as web_router

logger = logging.getLogger("partner_agent")
DB_READINESS_TIMEOUT_SECONDS = 5.0
STORAGE_READINESS_TIMEOUT_SECONDS = 10.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_settings()
    yield
    await get_engine().dispose()


app = FastAPI(title="Partner-Agent", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(admin_router)
app.include_router(groups_router)
app.include_router(chats_router)
app.include_router(chat_files_router)
app.include_router(runs_router)
app.include_router(documents_router)
app.include_router(design_router)
app.include_router(web_router)
app.include_router(accounting_router)
app.include_router(vendor_wish_router)


@app.middleware("http")
async def request_id(request: Request, call_next: RequestResponseEndpoint) -> Response:
    request.state.request_id = str(uuid.uuid4())
    try:
        response = await call_next(request)
    except Exception as exc:
        # ServerErrorMiddleware re-raises after calling a global 500 handler.
        # Return here so Uvicorn cannot log an exception containing private inputs.
        response = await unexpected_error(request, exc)
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def error_response(
    request: Request, status: int, code: str, message: str, retryable: bool = False
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", str(uuid.uuid4())),
            "retryable": retryable,
            "details": {},
        },
    )


@app.exception_handler(APIError)
async def api_error(request: Request, exc: APIError) -> JSONResponse:
    return error_response(request, exc.status, exc.code, exc.message, exc.retryable)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Pydantic errors may include input passwords. Do not echo or log them.
    return error_response(request, 422, "validation_error", "Invalid request")


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
    return error_response(request, exc.status_code, "http_error", "Request failed")


@app.exception_handler(SQLAlchemyError)
async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error("database_unavailable request_id=%s", request.state.request_id)
    return error_response(request, 503, "dependency_unavailable", "Database unavailable", True)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unexpected_error request_id=%s type=%s",
        getattr(request.state, "request_id", "unknown"),
        type(exc).__name__,
    )
    return error_response(request, 500, "internal_error", "Internal server error")


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "alive"}


def check_storage() -> None:
    settings = get_settings()
    client = create_s3_client(
        settings.s3_endpoint_url,
        settings.s3_region,
        settings.s3_access_key_id,
        settings.s3_secret_access_key.get_secret_value(),
        connect_timeout=2,
        read_timeout=2,
        max_attempts=1,
    )
    try:
        Storage(client, settings.s3_originals_bucket, settings.s3_derived_bucket).ready()
    finally:
        client.close()


@app.get("/health/ready")
async def ready(request: Request) -> Response:
    try:
        try:
            async with asyncio.timeout(DB_READINESS_TIMEOUT_SECONDS):
                async with get_engine().connect() as connection:
                    revision = await connection.scalar(
                        text("SELECT version_num FROM alembic_version")
                    )
                    if revision != "0029_render_timings":
                        return error_response(
                            request, 503, "schema_not_ready", "Migration required", True
                        )
        except TimeoutError:
            return error_response(
                request, 503, "dependency_timeout", "Database readiness timed out", True
            )
        settings = get_settings()
        if not settings.s3_access_key_id or not settings.s3_secret_access_key.get_secret_value():
            return error_response(
                request, 503, "storage_not_configured", "Storage credentials required", True
            )
        async with asyncio.timeout(STORAGE_READINESS_TIMEOUT_SECONDS):
            await to_thread.run_sync(check_storage, abandon_on_cancel=True)
        return JSONResponse({"status": "ready", "schema": revision, "storage": "ready"})
    except SQLAlchemyError:
        return error_response(request, 503, "dependency_unavailable", "Database unavailable", True)
    except (BotoCoreError, ClientError, OSError, TimeoutError):
        return error_response(request, 503, "storage_unavailable", "Storage unavailable", True)
