import logging
from enum import StrEnum
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from redaction import redact, redact_text


class ErrorCode(StrEnum):
    AUTH_ERROR = "auth_error"
    VALIDATION_ERROR = "validation_error"
    HTTP_ERROR = "http_error"
    CONFIG_ERROR = "config_error"
    RAG_ERROR = "rag_error"
    UPSTREAM_ERROR = "upstream_error"
    STORAGE_ERROR = "storage_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"
    RATE_LIMITED = "rate_limited"


logger = logging.getLogger(__name__)


class ApiError(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ApiError


class AppError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict[str, Any] | None = None,
        internal_message: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(internal_message or message)
        self.message = redact_text(message)
        self.code = str(code)
        self.status_code = status_code
        self.details = redact(details or {})
        self.headers = headers or {}


def error_response(status_code: int, code: ErrorCode | str, message: str, details: dict[str, Any] | None = None, headers=None):
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content=ErrorResponse(
            error=ApiError(code=str(code), message=redact_text(message), details=redact(details or {}))
        ).model_dump(),
    )


def app_error_handler(_request: Request, exc: AppError):
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


def http_error_handler(_request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    details = exc.detail if isinstance(exc.detail, dict) else {}
    return error_response(exc.status_code, ErrorCode.HTTP_ERROR, message, details, exc.headers)


def validation_error_handler(_request: Request, exc: RequestValidationError):
    return error_response(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        ErrorCode.VALIDATION_ERROR,
        "Request validation failed",
        # Validator contexts may contain exceptions, and input may contain passwords.
        # Return only the stable, JSON-safe diagnostic fields.
        {"errors": [{"type": error["type"], "loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()]},
    )


def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception(
        "unhandled_error",
        extra={"method": request.method, "path": request.url.path},
    )
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        ErrorCode.INTERNAL_ERROR,
        "Internal server error",
    )
