import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from errors import AppError, app_error_handler, http_error_handler, validation_error_handler
from routers import admin, auth_routes, chat, documents, qa, system
from settings import validate_runtime_config
from structured_logging import configure_logging

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("app.requests")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="Forest Department Pilot RAG", lifespan=lifespan)
    app.middleware("http")(log_requests)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(HTTPException, http_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.include_router(system.router)
    app.include_router(auth_routes.router)
    app.include_router(documents.router)
    app.include_router(qa.router)
    app.include_router(chat.router)
    app.include_router(admin.router)
    return app


async def log_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        request_logger.exception(
            "request_failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "client_ip": request.client.host if request.client else None,
                "duration_ms": duration_ms,
            },
        )
        raise
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    request_logger.info(
        "request_completed",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "client_ip": request.client.host if request.client else None,
            "duration_ms": duration_ms,
        },
    )
    return response


def allowed_cors_origins() -> list[str]:
    raw_origins = os.getenv("CORS_ALLOWED_ORIGINS")
    if not raw_origins:
        return ["http://localhost:3000", "http://127.0.0.1:3000", "*"]
    raw = raw_origins.strip()
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.getenv("VALIDATE_CONFIG_ON_STARTUP", "false").strip().lower() in {"1", "true", "yes"}:
        result = validate_runtime_config()
        if not result["ok"]:
            logger.error(
                "runtime_config_invalid",
                extra={"missing": result["missing"], "invalid": result["invalid"]},
            )
            raise RuntimeError(f"Invalid runtime configuration: {', '.join(result['missing'])}")
    logger.info("application_started")
    yield
    logger.info("application_stopped")


app = create_app()
