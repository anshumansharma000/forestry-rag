import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Literal

import requests

from errors import AppError, ErrorCode
from generation_cost import generation_cost
from settings import embedding_dimensions, env_int, gemini_api_key
from token_usage import record_generation, record_unmetered_attempt

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
EMBEDDING_2_MODEL = "gemini-embedding-2"


def _env_nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AppError(f"{name} must be an integer.", code=ErrorCode.CONFIG_ERROR) from exc
    if value < 0:
        raise AppError(f"{name} must be 0 or greater.", code=ErrorCode.CONFIG_ERROR)
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise AppError(f"{name} must be a number.", code=ErrorCode.CONFIG_ERROR) from exc


@dataclass(frozen=True)
class GenerationProfile:
    model_env: str
    default_model: str
    temperature_env: str
    default_temperature: float
    max_tokens_env: str
    default_max_tokens: int
    thinking_env: str
    default_thinking: str
    timeout_env: str
    default_timeout: int
    rate_bucket: str


GENERATION_PROFILES: dict[str, GenerationProfile] = {
    "rewrite_context": GenerationProfile(
        "GEMINI_UTILITY_MODEL", "gemini-3.5-flash-lite",
        "GEMINI_REWRITE_TEMPERATURE", 0.0,
        "GEMINI_CONTEXT_REWRITE_MAX_OUTPUT_TOKENS", 600,
        "GEMINI_REWRITE_THINKING_LEVEL", "minimal",
        "GEMINI_UTILITY_TIMEOUT_SECONDS", 30,
        "utility",
    ),
    "rewrite": GenerationProfile(
        "GEMINI_UTILITY_MODEL", "gemini-3.5-flash-lite",
        "GEMINI_REWRITE_TEMPERATURE", 0.0,
        "GEMINI_REWRITE_MAX_OUTPUT_TOKENS", 200,
        "GEMINI_REWRITE_THINKING_LEVEL", "minimal",
        "GEMINI_UTILITY_TIMEOUT_SECONDS", 30,
        "utility",
    ),
    "plan": GenerationProfile(
        "GEMINI_UTILITY_MODEL", "gemini-3.5-flash-lite",
        "GEMINI_PLAN_TEMPERATURE", 0.0,
        "GEMINI_PLAN_MAX_OUTPUT_TOKENS", 1200,
        "GEMINI_PLAN_THINKING_LEVEL", "low",
        "GEMINI_UTILITY_TIMEOUT_SECONDS", 45,
        "utility",
    ),
    "answer_direct": GenerationProfile(
        "GEMINI_DIRECT_MODEL", "gemini-3.5-flash-lite",
        "GEMINI_DIRECT_TEMPERATURE", 0.1,
        "GEMINI_DIRECT_MAX_OUTPUT_TOKENS", 1800,
        "GEMINI_DIRECT_THINKING_LEVEL", "minimal",
        "GEMINI_DIRECT_TIMEOUT_SECONDS", 60,
        "direct",
    ),
    "answer_complex": GenerationProfile(
        "GEMINI_COMPLEX_MODEL", "gemini-3.8-flash",
        "GEMINI_COMPLEX_TEMPERATURE", 0.1,
        "GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", 4000,
        "GEMINI_COMPLEX_THINKING_LEVEL", "medium",
        "GEMINI_COMPLEX_TIMEOUT_SECONDS", 120,
        "complex",
    ),
    "answer_complex_high": GenerationProfile(
        "GEMINI_COMPLEX_MODEL", "gemini-3.8-flash",
        "GEMINI_COMPLEX_TEMPERATURE", 0.1,
        "GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", 4000,
        "GEMINI_COMPLEX_HIGH_THINKING_LEVEL", "high",
        "GEMINI_COMPLEX_TIMEOUT_SECONDS", 120,
        "complex",
    ),
    "verify": GenerationProfile(
        "GEMINI_VERIFICATION_MODEL", "gemini-3.5-flash-lite",
        "GEMINI_VERIFY_TEMPERATURE", 0.0,
        "GEMINI_VERIFY_MAX_OUTPUT_TOKENS", 3000,
        "GEMINI_VERIFY_THINKING_LEVEL", "low",
        "GEMINI_VERIFICATION_TIMEOUT_SECONDS", 60,
        "verification",
    ),
    "verify_complex": GenerationProfile(
        "GEMINI_COMPLEX_MODEL", "gemini-3.8-flash",
        "GEMINI_VERIFY_TEMPERATURE", 0.0,
        "GEMINI_COMPLEX_MAX_OUTPUT_TOKENS", 4000,
        "GEMINI_COMPLEX_THINKING_LEVEL", "medium",
        "GEMINI_COMPLEX_TIMEOUT_SECONDS", 120,
        "complex",
    ),
}


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    operation: str
    finish_reason: str | None
    usage_metadata: dict[str, Any]


class GeminiClient:
    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds
        self._request_locks: dict[str, threading.Lock] = {}
        self._last_request_started: dict[str, float] = {}
        self._state_lock = threading.Lock()

    def _bucket_lock(self, bucket: str) -> threading.Lock:
        with self._state_lock:
            return self._request_locks.setdefault(bucket, threading.Lock())

    @staticmethod
    def _minimum_interval(bucket: str) -> float:
        specific = f"GEMINI_{bucket.upper()}_REQUEST_INTERVAL_MS"
        if os.getenv(specific) is not None:
            return _env_nonnegative_int(specific, 0) / 1000
        if bucket == "embedding":
            return _env_nonnegative_int("GEMINI_EMBEDDING_REQUEST_INTERVAL_MS", 250) / 1000
        return _env_nonnegative_int("GEMINI_GENERATION_REQUEST_INTERVAL_MS", 0) / 1000

    def _pace_requests(self, bucket: str) -> None:
        """Pace independent workloads without making ingestion block user answers."""
        minimum_interval = self._minimum_interval(bucket)
        if minimum_interval <= 0:
            return
        lock = self._bucket_lock(bucket)
        with lock:
            elapsed = time.monotonic() - self._last_request_started.get(bucket, 0.0)
            if elapsed < minimum_interval:
                time.sleep(minimum_interval - elapsed)
            self._last_request_started[bucket] = time.monotonic()

    @staticmethod
    def _retry_delay(response, retry_number: int, bucket: str) -> float:
        workload = "EMBEDDING" if bucket == "embedding" else "GENERATION"
        maximum = env_int(
            f"GEMINI_{workload}_RETRY_MAX_SECONDS",
            env_int("GEMINI_RETRY_MAX_SECONDS", 60),
        )
        retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), float(maximum))
            except (TypeError, ValueError):
                pass
        base = env_int(
            f"GEMINI_{workload}_RETRY_BASE_SECONDS",
            env_int("GEMINI_RETRY_BASE_SECONDS", 2),
        )
        delay = min(maximum, base * (2 ** (retry_number - 1)))
        return delay + random.uniform(0, min(1.0, delay * 0.25))

    def _post(
        self,
        model: str,
        action: str,
        payload: dict,
        timeout: int | None = None,
        max_retries: int | None = None,
        *,
        bucket: str | None = None,
        operation: str | None = None,
    ) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}"
        rate_bucket = bucket or ("embedding" if "embed" in action.lower() else "utility")
        workload = "EMBEDDING" if rate_bucket == "embedding" else "GENERATION"
        if max_retries is None:
            legacy_retries = _env_nonnegative_int("GEMINI_API_MAX_RETRIES", 5)
            max_retries = _env_nonnegative_int(f"GEMINI_{workload}_MAX_RETRIES", legacy_retries)
        request_timeout = timeout or self.timeout_seconds
        legacy_deadline = env_int("GEMINI_REQUEST_DEADLINE_SECONDS", max(60, request_timeout * 2))
        deadline_seconds = env_int(f"GEMINI_{workload}_DEADLINE_SECONDS", legacy_deadline)
        deadline = time.monotonic() + deadline_seconds
        last_exception: Exception | None = None

        for attempt in range(max_retries + 1):
            self._pace_requests(rate_bucket)
            response = None
            try:
                response = requests.post(
                    url,
                    params={"key": gemini_api_key()},
                    json=payload,
                    timeout=request_timeout,
                )
                if response.status_code < 400:
                    return response.json()
                if action == "generateContent" and (response.status_code == 408 or response.status_code >= 500):
                    record_unmetered_attempt()
                retryable = response.status_code in RETRYABLE_STATUS_CODES
            except requests.RequestException as exc:
                if action == "generateContent":
                    record_unmetered_attempt()
                last_exception = exc
                retryable = True

            if retryable and attempt < max_retries:
                retry_number = attempt + 1
                delay = self._retry_delay(response, retry_number, rate_bucket)
                if time.monotonic() + delay >= deadline:
                    break
                logger.warning(
                    "gemini_request_retrying",
                    extra={
                        "status_code": getattr(response, "status_code", None),
                        "action": action,
                        "operation": operation,
                        "model": model,
                        "retry": retry_number,
                        "max_retries": max_retries,
                        "delay_seconds": round(delay, 2),
                    },
                )
                time.sleep(delay)
                continue

            if response is None:
                raise AppError(
                    "Gemini API request failed.",
                    code=ErrorCode.UPSTREAM_ERROR,
                    status_code=502,
                    internal_message=f"Gemini API request failed: {last_exception}",
                ) from last_exception
            raise AppError(
                "Gemini API quota is exhausted. Try again after the quota resets."
                if response.status_code == 429
                else "Gemini API returned an error.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"status_code": response.status_code},
                internal_message=f"Gemini API failed: {response.status_code} {response.text}",
            )

        raise AppError(
            "Gemini API request did not complete within the retry deadline.",
            code=ErrorCode.UPSTREAM_ERROR,
            status_code=502,
            internal_message=f"Gemini API retry deadline exceeded: {last_exception}",
        ) from last_exception

    @staticmethod
    def _embedding_request(text: str, task_type: str, dimensions: int, model: str) -> dict:
        request = {
            "content": {"parts": [{"text": text}]},
            "outputDimensionality": dimensions,
        }
        if model != EMBEDDING_2_MODEL:
            request["taskType"] = task_type
        return request

    def embed(self, text: str, task_type: Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]) -> list[float]:
        model = os.getenv("GEMINI_EMBEDDING_MODEL", EMBEDDING_2_MODEL)
        expected_dimensions = embedding_dimensions()
        data = self._post(
            model,
            "embedContent",
            self._embedding_request(text, task_type, expected_dimensions, model),
            timeout=env_int("GEMINI_EMBEDDING_TIMEOUT_SECONDS", 45),
            bucket="embedding",
            operation="embedding",
        )
        try:
            values = data["embedding"]["values"]
        except KeyError as exc:
            raise AppError("Unexpected Gemini embedding response.", code=ErrorCode.UPSTREAM_ERROR, status_code=502) from exc
        self._validate_embedding(values, expected_dimensions)
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
        model = os.getenv("GEMINI_EMBEDDING_MODEL", EMBEDDING_2_MODEL)
        expected_dimensions = embedding_dimensions()
        requests_payload = []
        for text in texts:
            request = self._embedding_request(text, task_type, expected_dimensions, model)
            request["model"] = f"models/{model}"
            requests_payload.append(request)
        data = self._post(
            model,
            "batchEmbedContents",
            {"requests": requests_payload},
            timeout=env_int("GEMINI_EMBEDDING_TIMEOUT_SECONDS", 45),
            bucket="embedding",
            operation="embedding_batch",
        )
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
            self._validate_embedding(values, expected_dimensions)
        return embeddings

    @staticmethod
    def _validate_embedding(values: list[float], expected_dimensions: int) -> None:
        if len(values) != expected_dimensions:
            raise AppError(
                "Gemini embedding dimensions do not match runtime configuration.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"actual": len(values), "expected": expected_dimensions},
            )

    @staticmethod
    def _profile(operation: str) -> GenerationProfile:
        try:
            return GENERATION_PROFILES[operation]
        except KeyError as exc:
            raise AppError(
                f"Unsupported Gemini generation operation: {operation}",
                code=ErrorCode.CONFIG_ERROR,
            ) from exc

    @staticmethod
    def _model_for_profile(profile: GenerationProfile) -> str:
        configured = os.getenv(profile.model_env, "").strip()
        return configured or profile.default_model

    def generate_result(
        self,
        prompt: str,
        *,
        operation: str = "answer_direct",
        system_instruction: str | None = None,
        response_schema: dict | None = None,
    ) -> GenerationResult:
        profile = self._profile(operation)
        model = self._model_for_profile(profile)
        temperature = _env_float(profile.temperature_env, profile.default_temperature)
        if not 0 <= temperature <= 2:
            raise AppError(
                f"{profile.temperature_env} must be between 0 and 2.",
                code=ErrorCode.CONFIG_ERROR,
            )
        thinking_level = os.getenv(profile.thinking_env, profile.default_thinking).strip().lower()
        if thinking_level not in {"minimal", "low", "medium", "high"}:
            raise AppError(
                f"{profile.thinking_env} must be minimal, low, medium, or high.",
                code=ErrorCode.CONFIG_ERROR,
            )
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": env_int(profile.max_tokens_env, profile.default_max_tokens),
        }
        if model.startswith("gemini-3"):
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        if response_schema is not None:
            generation_config.update(
                {
                    "responseMimeType": "application/json",
                    "responseJsonSchema": response_schema,
                }
            )
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        started = time.perf_counter()
        data = self._post(
            model,
            "generateContent",
            payload,
            timeout=env_int(profile.timeout_env, profile.default_timeout),
            bucket=profile.rate_bucket,
            operation=operation,
        )
        usage_metadata = data.get("usageMetadata") or {}
        query_id = record_generation(operation, model, usage_metadata)
        candidates = data.get("candidates")
        first_candidate = candidates[0] if isinstance(candidates, list) and candidates else None
        logged_finish_reason = first_candidate.get("finishReason") if isinstance(first_candidate, dict) else None
        logger.info(
            "gemini_generation_received",
            extra={
                "operation": operation,
                "query_id": query_id,
                "model": model,
                "finish_reason": logged_finish_reason,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "prompt_tokens": usage_metadata.get("promptTokenCount"),
                "output_tokens": usage_metadata.get("candidatesTokenCount"),
                "thinking_tokens": usage_metadata.get("thoughtsTokenCount"),
                "cached_tokens": usage_metadata.get("cachedContentTokenCount"),
                "total_tokens": usage_metadata.get("totalTokenCount"),
                **generation_cost(model, usage_metadata),
            },
        )
        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AppError("Unexpected Gemini generation response.", code=ErrorCode.UPSTREAM_ERROR, status_code=502) from exc
        text_parts = [part.get("text", "") for part in parts if not part.get("thought") and part.get("text")]
        text = "".join(text_parts).strip()
        finish_reason = candidate.get("finishReason")
        if not text:
            raise AppError(
                "Gemini returned an empty response.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"finish_reason": finish_reason},
            )
        if finish_reason == "MAX_TOKENS":
            raise AppError(
                "Gemini response was truncated before completion.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"finish_reason": finish_reason, "operation": operation},
            )
        return GenerationResult(text, model, operation, finish_reason, usage_metadata)

    def generate(
        self,
        prompt: str,
        *,
        operation: str = "answer_direct",
        system_instruction: str | None = None,
        response_schema: dict | None = None,
    ) -> str:
        return self.generate_result(
            prompt,
            operation=operation,
            system_instruction=system_instruction,
            response_schema=response_schema,
        ).text

    def generate_structured(
        self,
        prompt: str,
        schema: dict,
        *,
        operation: str,
        system_instruction: str | None = None,
        parse_retries: int = 1,
    ) -> dict:
        last_error: Exception | None = None
        for _attempt in range(parse_retries + 1):
            value = self.generate(
                prompt,
                operation=operation,
                system_instruction=system_instruction,
                response_schema=schema,
            )
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            if isinstance(parsed, dict):
                return parsed
            last_error = TypeError("Structured Gemini output was not an object")
        raise AppError(
            "Gemini returned invalid structured output.",
            code=ErrorCode.UPSTREAM_ERROR,
            status_code=502,
            internal_message=f"Gemini structured output parsing failed: {last_error}",
        ) from last_error


gemini_client = GeminiClient()
