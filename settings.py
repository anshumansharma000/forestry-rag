import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from errors import AppError, ErrorCode
from generation_cost import COST_POLICY_VERSION, positive_setting

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


def document_ai_ocr_settings() -> dict[str, str | int | list[str]]:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.getenv("DOCUMENT_AI_LOCATION", "").strip()
    processor_id = os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
    language_hints = [
        language.strip()
        for language in os.getenv("DOCUMENT_AI_OCR_LANGUAGE_HINTS", "en,hi").split(",")
        if language.strip()
    ]
    missing = [
        name
        for name, value in {
            "GOOGLE_CLOUD_PROJECT": project_id,
            "DOCUMENT_AI_LOCATION": location,
            "DOCUMENT_AI_PROCESSOR_ID": processor_id,
        }.items()
        if not value
    ]
    if missing:
        raise AppError(
            "Document AI OCR is not configured.",
            code=ErrorCode.CONFIG_ERROR,
            details={"missing": missing},
        )
    return {
        "project_id": project_id,
        "location": location,
        "processor_id": processor_id,
        "language_hints": language_hints,
        "timeout_seconds": env_int("DOCUMENT_AI_OCR_TIMEOUT_SECONDS", 60),
        "retry_attempts": env_int("DOCUMENT_AI_OCR_RETRY_ATTEMPTS", 3),
        "max_request_bytes": env_int("DOCUMENT_AI_OCR_MAX_REQUEST_BYTES", 40_000_000),
    }


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
    from jev_settings import status as jev_status

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    status = {
        **jev_status(),
        "gemini_api_key_configured": bool(gemini_key) and gemini_key != "your_api_key_here",
        "supabase_url_configured": bool(os.getenv("SUPABASE_URL", "").strip()),
        "supabase_service_role_key_configured": bool(service_key) and service_key != "your_service_role_key_here",
        "supabase_url_valid": False,
        "supabase_url_hint": None,
        "embedding_dimensions": embedding_dimensions(),
        "gemini_embedding_model": os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-2"),
        "gemini_direct_model": os.getenv("GEMINI_DIRECT_MODEL", "gemini-3.5-flash-lite"),
        "gemini_complex_model": os.getenv("GEMINI_COMPLEX_MODEL", "gemini-3.8-flash"),
        "gemini_utility_model": os.getenv("GEMINI_UTILITY_MODEL", "gemini-3.5-flash-lite"),
        "gemini_verification_model": os.getenv("GEMINI_VERIFICATION_MODEL", "gemini-3.5-flash-lite"),
        "auth_disabled": env_bool("AUTH_DISABLED"),
        "bootstrap_admin_token_configured": bool(os.getenv("BOOTSTRAP_ADMIN_TOKEN", "").strip()),
        "jwt_secret_key_configured": bool(os.getenv("JWT_SECRET_KEY", "").strip()),
        "document_storage_backend": document_storage_backend(),
        "r2_bucket_configured": bool(os.getenv("R2_BUCKET", "").strip()),
        "celery_broker_configured": bool((os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_URL") or "").strip()),
        "celery_require_worker_online": env_bool("CELERY_REQUIRE_WORKER_ONLINE", True),
        "pdf_extract_tables": env_bool("PDF_EXTRACT_TABLES", True),
        "document_ai_ocr_enabled": env_bool("DOCUMENT_AI_OCR_ENABLED"),
        "document_ai_project_configured": bool(os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()),
        "document_ai_location_configured": bool(os.getenv("DOCUMENT_AI_LOCATION", "").strip()),
        "document_ai_processor_configured": bool(os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()),
        "google_credentials_file_configured": bool(os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()),
        "max_pdf_pages": env_int("MAX_PDF_PAGES", 500),
        "max_extracted_chars": env_int("MAX_EXTRACTED_CHARS", 15_000_000),
        "max_document_chunks": env_int("MAX_DOCUMENT_CHUNKS", 3000),
        "ingest_batch_size": env_int("INGEST_BATCH_SIZE", 24),
        "gemini_embedding_batch_size": env_int("GEMINI_EMBEDDING_BATCH_SIZE", 2),
        "gemini_direct_max_output_tokens": env_int("GEMINI_DIRECT_MAX_OUTPUT_TOKENS", 1800),
        "gemini_complex_max_output_tokens": env_int("GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", 4000),
        "gemini_complex_high_max_output_tokens": env_int("GEMINI_COMPLEX_HIGH_MAX_OUTPUT_TOKENS", 8192),
        "gemini_truncation_recovery": env_bool("GEMINI_TRUNCATION_RECOVERY", True),
        "gemini_truncation_max_output_tokens": env_int("GEMINI_TRUNCATION_MAX_OUTPUT_TOKENS", 16384),
        "rag_evidence_planning": env_bool("RAG_EVIDENCE_PLANNING", True),
        "rag_answer_verification": env_bool("RAG_ANSWER_VERIFICATION", True),
        "cost_policy_version": COST_POLICY_VERSION,
        "rag_lite_extraction": env_bool("RAG_LITE_EXTRACTION", False),
        "rag_risk_based_verification": env_bool("RAG_RISK_BASED_VERIFICATION", False),
        "rag_legal_batch_embeddings": env_bool("RAG_LEGAL_BATCH_EMBEDDINGS", False),
        "rag_cost_optimizations": env_bool("RAG_COST_OPTIMIZATIONS", True),
        "rag_selective_planning": env_bool("RAG_SELECTIVE_PLANNING", True),
        "rag_selective_history": env_bool("RAG_SELECTIVE_HISTORY", True),
        "rag_selective_evidence": env_bool("RAG_SELECTIVE_EVIDENCE", True),
        "rag_selective_reasoning": env_bool("RAG_SELECTIVE_REASONING", True),
        "rag_selective_verification": env_bool("RAG_SELECTIVE_VERIFICATION", True),
        "cost_usd_to_inr": positive_setting("COST_USD_TO_INR", 90.0),
        "cost_query_max_inr": positive_setting("COST_QUERY_MAX_INR", 1.5),
        "rag_multi_query": env_bool("RAG_MULTI_QUERY", True),
        "rag_multi_query_max": env_int("RAG_MULTI_QUERY_MAX", 4),
        "rag_index_version": os.getenv("RAG_INDEX_VERSION", "4").strip() or "4",
        "rag_direct_contexts": env_int("RAG_DIRECT_CONTEXTS", 5),
        "rag_complex_contexts": env_int("RAG_OVERVIEW_CONTEXTS", 12),
    }
    try:
        validate_supabase_settings()
        status["supabase_url_valid"] = True
    except AppError as exc:
        status["supabase_url_hint"] = exc.message
    return status


def validate_runtime_config(require_auth: bool = True) -> dict:
    from jev_settings import invalid_settings

    status = config_status()
    missing = []
    invalid = invalid_settings()
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
    if not status["celery_broker_configured"]:
        missing.append("CELERY_BROKER_URL")
    if document_storage_backend() == "r2":
        try:
            r2_settings()
        except AppError as exc:
            missing.extend(exc.details.get("missing", ["valid R2 document storage settings"]))
    if status["document_ai_ocr_enabled"]:
        try:
            document_ai_ocr_settings()
        except AppError as exc:
            missing.extend(exc.details.get("missing", ["valid Document AI OCR settings"]))
        credentials_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if credentials_path and not Path(credentials_path).is_file():
            invalid.append("GOOGLE_APPLICATION_CREDENTIALS")
    for name in (
        "JWT_EXPIRES_MINUTES",
        "REFRESH_TOKEN_EXPIRES_DAYS",
        "RAG_DIRECT_CANDIDATES",
        "RAG_DIRECT_ANCHORS",
        "RAG_DIRECT_CONTEXTS",
        "RAG_DIRECT_CONTEXT_TOKENS",
        "RAG_PROCEDURE_CANDIDATES",
        "RAG_PROCEDURE_ANCHORS",
        "RAG_PROCEDURE_CONTEXTS",
        "RAG_PROCEDURE_CONTEXT_TOKENS",
        "RAG_COMPARISON_CANDIDATES",
        "RAG_COMPARISON_ANCHORS",
        "RAG_COMPARISON_CONTEXTS",
        "RAG_COMPARISON_CONTEXT_TOKENS",
        "RAG_OVERVIEW_CANDIDATES",
        "RAG_OVERVIEW_ANCHORS",
        "RAG_OVERVIEW_CONTEXTS",
        "RAG_OVERVIEW_CONTEXT_TOKENS",
        "RAG_TEMPORAL_CANDIDATES",
        "RAG_TEMPORAL_ANCHORS",
        "RAG_TEMPORAL_CONTEXTS",
        "RAG_TEMPORAL_CONTEXT_TOKENS",
        "CELERY_VISIBILITY_TIMEOUT_SECONDS",
        "CELERY_TASK_MAX_RETRIES",
        "CELERY_TASK_RETRY_BASE_SECONDS",
        "CELERY_WORKER_CONCURRENCY",
        "CELERY_WORKER_PING_TIMEOUT_SECONDS",
        "UPLOAD_MAX_BYTES",
        "UPLOAD_BATCH_MAX_BYTES",
        "UPLOAD_BATCH_MAX_FILES",
        "MAX_PDF_PAGES",
        "MAX_EXTRACTED_CHARS",
        "MAX_DOCUMENT_CHUNKS",
        "INGEST_BATCH_SIZE",
        "GEMINI_EMBEDDING_BATCH_SIZE",
        "GEMINI_API_MAX_RETRIES",
        "GEMINI_RETRY_BASE_SECONDS",
        "GEMINI_RETRY_MAX_SECONDS",
        "GEMINI_EMBEDDING_RETRY_BASE_SECONDS",
        "GEMINI_EMBEDDING_RETRY_MAX_SECONDS",
        "GEMINI_EMBEDDING_DEADLINE_SECONDS",
        "GEMINI_GENERATION_RETRY_BASE_SECONDS",
        "GEMINI_GENERATION_RETRY_MAX_SECONDS",
        "GEMINI_GENERATION_DEADLINE_SECONDS",
        "DOCUMENT_AI_OCR_MIN_TEXT_CHARS",
        "DOCUMENT_AI_OCR_MAX_PAGES",
        "DOCUMENT_AI_OCR_TIMEOUT_SECONDS",
        "DOCUMENT_AI_OCR_RETRY_ATTEMPTS",
        "DOCUMENT_AI_OCR_MAX_REQUEST_BYTES",
        "GEMINI_DIRECT_MAX_OUTPUT_TOKENS",
        "GEMINI_COMPLEX_MAX_OUTPUT_TOKENS",
        "GEMINI_COMPLEX_HIGH_MAX_OUTPUT_TOKENS",
        "GEMINI_TRUNCATION_MAX_OUTPUT_TOKENS",
        "GEMINI_REWRITE_MAX_OUTPUT_TOKENS",
        "GEMINI_CONTEXT_REWRITE_MAX_OUTPUT_TOKENS",
        "RAG_HISTORY_SELECTION_TOKENS",
        "GEMINI_PLAN_MAX_OUTPUT_TOKENS",
        "GEMINI_VERIFY_MAX_OUTPUT_TOKENS",
    ):
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
    raw = os.getenv("RETRIEVAL_MAX_PER_SOURCE")
    if raw is not None and raw.strip():
        try:
            value = int(raw)
        except ValueError:
            invalid.append("RETRIEVAL_MAX_PER_SOURCE")
        else:
            if value < 0:
                invalid.append("RETRIEVAL_MAX_PER_SOURCE")
    for name in (
        "GEMINI_EMBEDDING_REQUEST_INTERVAL_MS",
        "GEMINI_GENERATION_REQUEST_INTERVAL_MS",
        "GEMINI_UTILITY_REQUEST_INTERVAL_MS",
        "GEMINI_DIRECT_REQUEST_INTERVAL_MS",
        "GEMINI_COMPLEX_REQUEST_INTERVAL_MS",
        "GEMINI_VERIFICATION_REQUEST_INTERVAL_MS",
        "GEMINI_EMBEDDING_MAX_RETRIES",
        "GEMINI_GENERATION_MAX_RETRIES",
    ):
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = int(raw)
        except ValueError:
            invalid.append(name)
            continue
        if value < 0:
            invalid.append(name)
    for name in (
        "GEMINI_REWRITE_THINKING_LEVEL",
        "GEMINI_PLAN_THINKING_LEVEL",
        "GEMINI_DIRECT_THINKING_LEVEL",
        "GEMINI_COMPLEX_THINKING_LEVEL",
        "GEMINI_COMPLEX_HIGH_THINKING_LEVEL",
        "GEMINI_VERIFY_THINKING_LEVEL",
    ):
        raw = os.getenv(name)
        if raw and raw.strip().lower() not in {"minimal", "low", "medium", "high"}:
            invalid.append(name)
    for name in ("RETRIEVAL_DUPLICATE_THRESHOLD", "RETRIEVAL_MIN_CONTEXT_SCORE", "RETRIEVAL_CONFIDENCE_THRESHOLD"):
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        try:
            value = float(raw)
        except ValueError:
            invalid.append(name)
            continue
        if not 0 <= value <= 1:
            invalid.append(name)
    return {"ok": not missing and not invalid, "missing": missing, "invalid": invalid, "status": status}
