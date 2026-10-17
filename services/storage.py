import threading
from functools import lru_cache

from supabase import Client, create_client

from errors import AppError, ErrorCode
from settings import validate_supabase_settings

_thread_clients = threading.local()
_owned_clients = []
_client_lock = threading.Lock()
_client_epoch = 0


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


def thread_supabase_client():
    if getattr(_thread_clients, "epoch", None) != _client_epoch:
        url, key = validate_supabase_settings()
        _thread_clients.client = create_client(url, key)
        _thread_clients.epoch = _client_epoch
        with _client_lock:
            _owned_clients.append(_thread_clients.client)
    return _thread_clients.client


def close_thread_clients():
    global _client_epoch
    with _client_lock:
        for client in _owned_clients:
            postgrest = getattr(client, "_postgrest", None)
            if postgrest is not None:
                postgrest.session.close()
        _owned_clients.clear()
        _client_epoch += 1
