import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from errors import AppError, ErrorCode

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "data" / "docs"

load_dotenv(dotenv_path=ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AppError(f"{name} must be an integer.", code=ErrorCode.CONFIG_ERROR) from exc
    if value <= 0:
        raise AppError(f"{name} must be greater than 0.", code=ErrorCode.CONFIG_ERROR)
    return value


def document_storage_backend() -> str:
    return os.getenv("DOCUMENT_STORAGE_BACKEND", "local").strip().lower() or "local"


def r2_settings() -> dict[str, str]:
    account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
    access_key_id = os.getenv("R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
    bucket = os.getenv("R2_BUCKET", "").strip()
    prefix = os.getenv("R2_PREFIX", "docs/").strip().lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"

    missing = [
        name
        for name, value in {
            "R2_ACCOUNT_ID": account_id,
            "R2_ACCESS_KEY_ID": access_key_id,
            "R2_SECRET_ACCESS_KEY": secret_access_key,
            "R2_BUCKET": bucket,
        }.items()
        if not value
    ]
    if missing:
        raise AppError(
            "R2 document storage is not configured.",
            code=ErrorCode.CONFIG_ERROR,
            details={"missing": missing},
        )

    return {
        "account_id": account_id,
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "bucket": bucket,
        "prefix": prefix,
        "endpoint_url": f"https://{account_id}.r2.cloudflarestorage.com",
    }


def gemini_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key == "your_api_key_here":
        raise AppError(
            "GEMINI_API_KEY is not configured. Copy .env.example to .env and add your key.",
            code=ErrorCode.CONFIG_ERROR,
        )
    return key


def embedding_dimensions() -> int:
    return env_int("EMBEDDING_DIMENSIONS", 768)


def validate_supabase_settings() -> tuple[str, str]:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or "your-project-ref" in url:
        raise AppError("SUPABASE_URL is not configured. Add your Supabase project URL to .env.", code=ErrorCode.CONFIG_ERROR)
    if url.startswith(("postgres://", "postgresql://")):
        parsed = urlparse(url)
        host = parsed.hostname or ""
        project_ref = host.removeprefix("db.").removesuffix(".supabase.co")
        suggested = f"https://{project_ref}.supabase.co" if project_ref and project_ref != host else "https://<project-ref>.supabase.co"
        raise AppError(
            "SUPABASE_URL must be the Supabase project API URL, not the Postgres database connection string.",
            code=ErrorCode.CONFIG_ERROR,
            details={"suggested_url": suggested},
        )
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(".supabase.co"):
        raise AppError("SUPABASE_URL must look like https://<project-ref>.supabase.co", code=ErrorCode.CONFIG_ERROR)
    if not key or key == "your_service_role_key_here":
        raise AppError(
            "SUPABASE_SERVICE_ROLE_KEY is not configured. Add your service role key to .env.",
            code=ErrorCode.CONFIG_ERROR,
        )
    return url, key


def config_status() -> dict:
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    status = {
        "gemini_api_key_configured": bool(gemini_key) and gemini_key != "your_api_key_here",
        "supabase_url_configured": bool(os.getenv("SUPABASE_URL", "").strip()),
        "supabase_service_role_key_configured": bool(service_key) and service_key != "your_service_role_key_here",
        "supabase_url_valid": False,
        "supabase_url_hint": None,
        "embedding_dimensions": embedding_dimensions(),
        "auth_disabled": env_bool("AUTH_DISABLED"),
        "bootstrap_admin_token_configured": bool(os.getenv("BOOTSTRAP_ADMIN_TOKEN", "").strip()),
        "jwt_secret_key_configured": bool(os.getenv("JWT_SECRET_KEY", "").strip()),
        "document_storage_backend": document_storage_backend(),
        "r2_bucket_configured": bool(os.getenv("R2_BUCKET", "").strip()),
    }
    try:
        validate_supabase_settings()
        status["supabase_url_valid"] = True
    except AppError as exc:
        status["supabase_url_hint"] = exc.message
    return status


def validate_runtime_config(require_auth: bool = True) -> dict:
    status = config_status()
    missing = []
    invalid = []
    if not status["gemini_api_key_configured"]:
        missing.append("GEMINI_API_KEY")
    if not status["supabase_url_configured"]:
        missing.append("SUPABASE_URL")
    if not status["supabase_service_role_key_configured"]:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    if not status["supabase_url_valid"]:
        missing.append("valid SUPABASE_URL")
    if require_auth and not status["auth_disabled"] and not status["jwt_secret_key_configured"]:
        missing.append("JWT_SECRET_KEY or AUTH_DISABLED=true")
    if document_storage_backend() == "r2":
        try:
            r2_settings()
        except AppError as exc:
            missing.extend(exc.details.get("missing", ["valid R2 document storage settings"]))
    for name in ("JWT_EXPIRES_MINUTES", "REFRESH_TOKEN_EXPIRES_DAYS"):
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError:
            invalid.append(name)
            continue
        if value <= 0:
            invalid.append(name)
    return {"ok": not missing and not invalid, "missing": missing, "invalid": invalid, "status": status}
