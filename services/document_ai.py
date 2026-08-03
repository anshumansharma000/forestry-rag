import logging
import time
from typing import Any

from errors import AppError, ErrorCode
from settings import document_ai_ocr_settings

logger = logging.getLogger(__name__)

RETRYABLE_EXCEPTION_NAMES = {
    "DeadlineExceeded",
    "InternalServerError",
    "ResourceExhausted",
    "ServiceUnavailable",
    "TooManyRequests",
}


class DocumentAIClient:
    def __init__(self, client: Any | None = None, documentai_module: Any | None = None, sleep=time.sleep):
        self._client = client
        self._documentai_module = documentai_module
        self._sleep = sleep

    def ocr_pdf_page(self, pdf_content: bytes, *, source: str, page_number: int) -> str:
        settings = document_ai_ocr_settings()
        max_request_bytes = int(settings["max_request_bytes"])
        if len(pdf_content) > max_request_bytes:
            raise AppError(
                "PDF page is too large for the configured OCR request limit.",
                code=ErrorCode.INVALID_INPUT,
                details={"source": source, "page": page_number, "max_bytes": max_request_bytes},
            )

        if self._documentai_module is None:
            self._documentai_module = self._load_documentai_module()
        documentai = self._documentai_module
        if self._client is None:
            self._client = self._build_client(documentai, str(settings["location"]))
        client = self._client
        name = client.processor_path(
            str(settings["project_id"]),
            str(settings["location"]),
            str(settings["processor_id"]),
        )
        language_hints = list(settings["language_hints"])
        ocr_config = documentai.OcrConfig(
            enable_native_pdf_parsing=True,
            hints=documentai.OcrConfig.Hints(language_hints=language_hints),
        )
        request = documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(content=pdf_content, mime_type="application/pdf"),
            process_options=documentai.ProcessOptions(ocr_config=ocr_config),
        )

        started = time.perf_counter()
        attempts = int(settings["retry_attempts"])
        for attempt in range(1, attempts + 1):
            try:
                response = client.process_document(request=request, timeout=int(settings["timeout_seconds"]))
                text = (getattr(response.document, "text", "") or "").strip()
                logger.info(
                    "document_ai_ocr_page_processed",
                    extra={
                        "source": source,
                        "page": page_number,
                        "attempt": attempt,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                        "extracted_chars": len(text),
                    },
                )
                return text
            except Exception as exc:
                if attempt < attempts and self._is_retryable(exc):
                    self._sleep(min(2 ** (attempt - 1), 8))
                    continue
                raise AppError(
                    "Document OCR service failed.",
                    code=ErrorCode.UPSTREAM_ERROR,
                    status_code=502,
                    details={"source": source, "page": page_number},
                    internal_message=f"Document AI OCR failed for {source} page {page_number}: {exc}",
                ) from exc

        raise AssertionError("Document AI OCR retry loop exited unexpectedly")

    @staticmethod
    def _load_documentai_module():
        try:
            from google.cloud import documentai
        except ImportError as exc:
            raise AppError(
                "Document AI OCR dependency is not installed.",
                code=ErrorCode.CONFIG_ERROR,
            ) from exc
        return documentai

    @staticmethod
    def _build_client(documentai, location: str):
        try:
            from google.api_core.client_options import ClientOptions
        except ImportError as exc:
            raise AppError(
                "Document AI OCR dependency is not installed.",
                code=ErrorCode.CONFIG_ERROR,
            ) from exc
        return documentai.DocumentProcessorServiceClient(
            client_options=ClientOptions(api_endpoint=f"{location}-documentai.googleapis.com")
        )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        return exc.__class__.__name__ in RETRYABLE_EXCEPTION_NAMES


document_ai_client = DocumentAIClient()
