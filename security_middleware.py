"""Bound HTTP bodies before FastAPI/multipart parsing, including chunked requests."""

import asyncio
import re
from tempfile import SpooledTemporaryFile

from starlette.concurrency import run_in_threadpool

from errors import AppError, ErrorCode, error_response
from request_limits import acquire, bucket_for
from security_settings import positive_int


class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path, method = scope["path"].rstrip("/") or "/", scope["method"]
        if method == "OPTIONS" or (method == "GET" and path == "/health"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        lease = None
        spool = None
        try:
            target_length = len(scope.get("raw_path", path.encode())) + len(scope.get("query_string", b""))
            if target_length > positive_int("REQUEST_TARGET_MAX_BYTES", 8192):
                raise AppError("Request URL is too long.", code=ErrorCode.INVALID_INPUT, status_code=414)
            if sum(len(k) + len(v) for k, v in scope.get("headers", [])) > positive_int("REQUEST_HEADERS_MAX_BYTES", 32768):
                raise AppError("Request headers are too large.", code=ErrorCode.INVALID_INPUT, status_code=431)
            limit = (
                positive_int("AUTH_REQUEST_MAX_BYTES", 16384) if path.startswith("/auth/") else positive_int("REQUEST_MAX_BYTES", 1048576)
            )
            upload_path = path in {"/documents/upload", "/documents/uploads"} or re.fullmatch(
                r"/admin/rag-lab/experiments/[^/]+/files", path
            )
            if upload_path and method == "POST" and headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data"):
                limit = positive_int("UPLOAD_BATCH_MAX_BYTES", 150 * 1024 * 1024) + 1024 * 1024
            if b"content-length" in headers:
                try:
                    declared = int(headers[b"content-length"])
                except ValueError as exc:
                    raise AppError("Invalid Content-Length.", code=ErrorCode.INVALID_INPUT) from exc
                if declared < 0:
                    raise AppError("Invalid Content-Length.", code=ErrorCode.INVALID_INPUT)
                if declared > limit:
                    raise AppError("Request body is too large.", code=ErrorCode.INVALID_INPUT, status_code=413)
            identity = (scope.get("client") or ("unknown", 0))[0]
            lease = await run_in_threadpool(acquire, bucket_for(method, path), identity, by_ip=True)
            spool = SpooledTemporaryFile(max_size=1024 * 1024)
            total = 0
            async with asyncio.timeout(positive_int("REQUEST_BODY_TIMEOUT_SECONDS", 120)):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        spool.close()
                        if lease:
                            await run_in_threadpool(lease.close)
                        return
                    body = message.get("body", b"")
                    total += len(body)
                    if total > limit:
                        raise AppError("Request body is too large.", code=ErrorCode.INVALID_INPUT, status_code=413)
                    await run_in_threadpool(spool.write, body)
                    if not message.get("more_body", False):
                        break
            spool.seek(0)
        except (AppError, TimeoutError) as exc:
            if spool:
                spool.close()
            if lease:
                await run_in_threadpool(lease.close)
            error = exc if isinstance(exc, AppError) else AppError("Request body timed out.", code=ErrorCode.INVALID_INPUT, status_code=408)
            return await error_response(error.status_code, error.code, error.message, error.details, error.headers)(scope, receive, send)
        except BaseException:
            if spool:
                spool.close()
            if lease:
                await run_in_threadpool(lease.close)
            raise

        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if delivered:
                return await receive()
            part = await run_in_threadpool(spool.read, 1024 * 1024)
            more = spool.tell() < total
            delivered = not more
            return {"type": "http.request", "body": part, "more_body": more}

        try:
            await self.app(scope, bounded_receive, send)
        finally:
            spool.close()
            if lease:
                await run_in_threadpool(lease.close)
