from functools import lru_cache

from supabase import Client, create_client

from errors import AppError, ErrorCode
from settings import validate_supabase_settings


@lru_cache(maxsize=1)
def supabase_client() -> Client:
    url, key = validate_supabase_settings()
    try:
        return create_client(url, key)
    except Exception as exc:
        raise AppError(
            "Could not initialize Supabase client.",
            code=ErrorCode.STORAGE_ERROR,
            internal_message=f"Could not initialize Supabase client: {exc}",
        ) from exc
