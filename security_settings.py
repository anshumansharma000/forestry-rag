"""Security settings are read at runtime; importing this module never opens Redis."""

import os
from urllib.parse import parse_qs, urlparse

from errors import AppError, ErrorCode


def enabled(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes"}


def is_production() -> bool:
    return os.getenv("APP_ENV", "").strip().lower() in {"production", "prod"} or enabled("RENDER")


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise AppError(f"{name} must be a positive integer.", code=ErrorCode.CONFIG_ERROR) from exc
    if value <= 0:
        raise AppError(f"{name} must be a positive integer.", code=ErrorCode.CONFIG_ERROR)
    return value


def limits_enabled() -> bool:
    return enabled("RATE_LIMIT_ENABLED", is_production())


def redis_url() -> str:
    return (os.getenv("SECURITY_REDIS_URL") or os.getenv("REDIS_URL") or os.getenv("CELERY_BROKER_URL") or "").strip()


def validate_security_settings() -> None:
    if is_production():
        if enabled("AUTH_DISABLED"):
            raise AppError("AUTH_DISABLED must be false in production.", code=ErrorCode.CONFIG_ERROR)
        secret = os.getenv("JWT_SECRET_KEY", "").strip()
        if len(secret.encode()) < 32 or secret.lower().startswith(("replace_", "your_", "change-me", "changeme")):
            raise AppError("Production JWT_SECRET_KEY must contain at least 32 bytes.", code=ErrorCode.CONFIG_ERROR)
        if not limits_enabled():
            raise AppError("Distributed request limits must be enabled in production.", code=ErrorCode.CONFIG_ERROR)
    if limits_enabled() and not redis_url():
        raise AppError("SECURITY_REDIS_URL or a Redis broker URL is required for request limits.", code=ErrorCode.CONFIG_ERROR)
    if limits_enabled():
        parsed = urlparse(redis_url())
        if parsed.scheme not in {"redis", "rediss", "unix"}:
            raise AppError("Request limits require a Redis URL.", code=ErrorCode.CONFIG_ERROR)
        if is_production() and parsed.scheme == "rediss":
            query = parse_qs(parsed.query)
            if query.get("ssl_cert_reqs", ["required"]) != ["required"]:
                raise AppError("Redis TLS certificate verification must be required.", code=ErrorCode.CONFIG_ERROR)
        from request_limits import DEFAULTS

        for bucket, (ip_rate, user_rate, concurrency, individual) in DEFAULTS.items():
            positive_int(f"LIMIT_{bucket.upper()}_IP_PER_MINUTE", ip_rate)
            positive_int(f"LIMIT_{bucket.upper()}_USER_PER_MINUTE", user_rate)
            if concurrency:
                positive_int(f"LIMIT_{bucket.upper()}_GLOBAL_CONCURRENCY", concurrency)
                suffix = "IP" if bucket in {"auth", "upload"} else "USER"
                positive_int(f"LIMIT_{bucket.upper()}_{suffix}_CONCURRENCY", individual)
    for name, default in {
        "REQUEST_MAX_BYTES": 1_048_576,
        "AUTH_REQUEST_MAX_BYTES": 16_384,
        "REQUEST_BODY_TIMEOUT_SECONDS": 120,
        "REQUEST_HEADERS_MAX_BYTES": 32_768,
        "REQUEST_TARGET_MAX_BYTES": 8192,
        "LIMIT_LEASE_SECONDS": 120,
        "LIMIT_WORKER_GLOBAL_CONCURRENCY": 2,
        "UPLOAD_BATCH_MAX_BYTES": 157286400,
    }.items():
        positive_int(name, default)
    if positive_int("LIMIT_LEASE_SECONDS", 120) < 10:
        raise AppError("LIMIT_LEASE_SECONDS must be at least 10.", code=ErrorCode.CONFIG_ERROR)
