import logging
import os
import random
import threading
import time
from typing import Literal

import requests

from errors import AppError, ErrorCode
from settings import embedding_dimensions, env_int, gemini_api_key

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429}


class GeminiClient:
    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self._request_lock = threading.Lock()
        self._last_request_started = 0.0

    def _pace_requests(self) -> None:
        """Keep a single process from bursting through Gemini's per-minute quota."""
        minimum_interval = env_int("GEMINI_REQUEST_INTERVAL_MS", 4000) / 1000
        with self._request_lock:
            elapsed = time.monotonic() - self._last_request_started
            if elapsed < minimum_interval:
                time.sleep(minimum_interval - elapsed)
            self._last_request_started = time.monotonic()

    @staticmethod
    def _retry_delay(response, retry_number: int) -> float:
        retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), float(env_int("GEMINI_RETRY_MAX_SECONDS", 60)))
            except (TypeError, ValueError):
                pass
        base = env_int("GEMINI_RETRY_BASE_SECONDS", 2)
        maximum = env_int("GEMINI_RETRY_MAX_SECONDS", 60)
        delay = min(maximum, base * (2 ** (retry_number - 1)))
        return delay + random.uniform(0, min(1.0, delay * 0.25))

    def _post(
        self,
        model: str,
        action: str,
        payload: dict,
        timeout: int | None = None,
        max_retries: int | None = None,
    ) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}"
        max_retries = env_int("GEMINI_API_MAX_RETRIES", 5) if max_retries is None else max_retries
        for attempt in range(max_retries + 1):
            self._pace_requests()
            try:
                response = requests.post(
                    url,
                    params={"key": gemini_api_key()},
                    json=payload,
                    timeout=timeout or self.timeout_seconds,
                )
            except requests.RequestException as exc:
                raise AppError(
                    "Gemini API request failed.",
                    code=ErrorCode.UPSTREAM_ERROR,
                    internal_message=f"Gemini API request failed: {exc}",
                ) from exc
            if response.status_code < 400:
                return response.json()
            if response.status_code in RETRYABLE_STATUS_CODES and attempt < max_retries:
                retry_number = attempt + 1
                delay = self._retry_delay(response, retry_number)
                logger.warning(
                    "gemini_request_retrying",
                    extra={
                        "status_code": response.status_code,
                        "action": action,
                        "retry": retry_number,
                        "max_retries": max_retries,
                        "delay_seconds": round(delay, 2),
                    },
                )
                time.sleep(delay)
                continue
            # Keep upstream bodies out of user-facing responses; logs/exception chaining retain context.
            raise AppError(
                "Gemini API quota is exhausted. Try again after the quota resets."
                if response.status_code == 429
                else "Gemini API returned an error.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"status_code": response.status_code},
                internal_message=f"Gemini API failed: {response.status_code} {response.text}",
            )
        raise AssertionError("Gemini retry loop exited unexpectedly")

    def embed(self, text: str, task_type: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]) -> list[float]:
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
        expected_dimensions = embedding_dimensions()
        data = self._post(
            model,
            "embedContent",
            {
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": expected_dimensions,
            },
        )
        try:
            values = data["embedding"]["values"]
        except KeyError as exc:
            raise AppError("Unexpected Gemini embedding response.", code=ErrorCode.UPSTREAM_ERROR, status_code=502) from exc
        if len(values) != expected_dimensions:
            raise AppError(
                "Gemini embedding dimensions do not match runtime configuration.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"actual": len(values), "expected": expected_dimensions},
            )
        return values

    def embed_many(
        self,
        texts: list[str],
        task_type: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"],
    ) -> list[list[float]]:
        if not texts:
            return []
        if len(texts) == 1:
            return [self.embed(texts[0], task_type)]
        model = os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
        expected_dimensions = embedding_dimensions()
        try:
            data = self._post(
                model,
                "batchEmbedContents",
                {
                    "requests": [
                        {
                            "model": f"models/{model}",
                            "content": {"parts": [{"text": text}]},
                            "taskType": task_type,
                            "outputDimensionality": expected_dimensions,
                        }
                        for text in texts
                    ]
                },
                # An oversized synchronous batch will remain oversized on retry.
                # Split it immediately; singleton calls retain normal 429 retries.
                max_retries=0,
            )
        except AppError as exc:
            if exc.details.get("status_code") != 429 or len(texts) == 1:
                raise
            midpoint = len(texts) // 2
            logger.warning(
                "gemini_embedding_batch_splitting",
                extra={"batch_size": len(texts), "left_size": midpoint, "right_size": len(texts) - midpoint},
            )
            return self.embed_many(texts[:midpoint], task_type) + self.embed_many(texts[midpoint:], task_type)
        try:
            embeddings = [item["values"] for item in data["embeddings"]]
        except (KeyError, TypeError) as exc:
            raise AppError("Unexpected Gemini batch embedding response.", code=ErrorCode.UPSTREAM_ERROR, status_code=502) from exc
        if len(embeddings) != len(texts):
            raise AppError(
                "Gemini batch embedding count does not match the request.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"actual": len(embeddings), "expected": len(texts)},
            )
        for values in embeddings:
            if len(values) != expected_dimensions:
                raise AppError(
                    "Gemini embedding dimensions do not match runtime configuration.",
                    code=ErrorCode.UPSTREAM_ERROR,
                    status_code=502,
                    details={"actual": len(values), "expected": expected_dimensions},
                )
        return embeddings

    def generate(self, prompt: str) -> str:
        model = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash-lite")
        data = self._post(
            model,
            "generateContent",
            {"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except (KeyError, IndexError) as exc:
            raise AppError("Unexpected Gemini chat response.", code=ErrorCode.UPSTREAM_ERROR, status_code=502) from exc


gemini_client = GeminiClient()
