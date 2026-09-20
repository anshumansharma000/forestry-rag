"""Shared redaction for API errors, durable failure messages and structured logs."""

import os
import re
from collections.abc import Mapping
from urllib.parse import quote

SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|token_hash)", re.I
)


def redact_text(value: str) -> str:
    text = str(value)
    # Also cover actual configured credentials when an upstream exception prints
    # them without a label. Never include the environment itself in diagnostics.
    for key, secret in os.environ.items():
        if SENSITIVE_KEY.search(key) or key.endswith(("BROKER_URL", "REDIS_URL", "DATABASE_URL", "_KEY", "_TOKEN", "_ACCESS_KEY_ID")):
            if len(secret) >= 8:
                text = text.replace(secret, "[REDACTED]").replace(quote(secret, safe=""), "[REDACTED]")
    text = re.sub(r'(?i)\bBearer\s+(?!token\b)[^\s,;"\']+', "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", text)
    text = re.sub(r'(?i)(https?|postgres(?:ql)?|rediss?)://[^\s"\'<>]+', "[REDACTED_URL]", text)
    text = re.sub(
        r"""(?ix)(["']?(?:password|passwd|secret|authorization|cookie|api[_-]?key|key|
                        access[_-]?token|refresh[_-]?token|token_hash)["']?\s*[:=]\s*)
                     (?:"[^"]*"|'[^']*'|[^\s,;&}]+)""",
        r"\1[REDACTED]",
        text,
    )
    # Absolute local paths are not useful in client errors or durable job failures.
    text = re.sub(r'(?<!\w)/(?:Users|home|private|tmp|etc|var)/[^\s"\'<>]+', "[REDACTED_PATH]", text)
    return text


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SENSITIVE_KEY.search(str(key)) and not str(key).endswith("_configured") else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    if value is None or isinstance(value, int | float | bool):
        return value
    return redact_text(str(value))


def safe_failure(exc: Exception) -> str:
    # Import lazily so errors.py can use this module too. Never persist str(exc):
    # AppError.__str__ may contain its private upstream diagnostic.
    from errors import AppError

    if isinstance(exc, AppError):
        return redact_text(exc.message)
    return "Operation failed. Please retry or contact an administrator."
