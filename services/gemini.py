import os
from typing import Literal

import requests

from errors import AppError, ErrorCode
from settings import embedding_dimensions, gemini_api_key


class GeminiClient:
    def __init__(self, timeout_seconds: int = 30):
        self.timeout_seconds = timeout_seconds

    def _post(self, model: str, action: str, payload: dict, timeout: int | None = None) -> dict:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{action}"
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
        if response.status_code >= 400:
            # Keep upstream bodies out of user-facing responses; logs/exception chaining retain context.
            raise AppError(
                "Gemini API returned an error.",
                code=ErrorCode.UPSTREAM_ERROR,
                status_code=502,
                details={"status_code": response.status_code},
                internal_message=f"Gemini API failed: {response.status_code} {response.text}",
            )
        return response.json()

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
