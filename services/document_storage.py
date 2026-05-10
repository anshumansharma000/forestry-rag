from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from errors import AppError, ErrorCode
from settings import DOCS_DIR, document_storage_backend, r2_settings

SUPPORTED_STORAGE_EXTENSIONS = {".txt", ".pdf", ".docx"}


@dataclass(frozen=True)
class StoredDocumentFile:
    name: str
    path: Path


class LocalDocumentStorage:
    backend = "local"

    def exists(self, filename: str) -> bool:
        return (DOCS_DIR / filename).exists()

    def save(self, filename: str, content: bytes) -> str:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        destination = DOCS_DIR / filename
        destination.write_bytes(content)
        return str(destination)

    @contextmanager
    def document_files(self) -> Iterator[list[StoredDocumentFile]]:
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        yield [
            StoredDocumentFile(name=path.name, path=path)
            for path in sorted(DOCS_DIR.iterdir())
            if path.suffix.lower() in SUPPORTED_STORAGE_EXTENSIONS
        ]


class R2DocumentStorage:
    backend = "r2"

    def __init__(self) -> None:
        settings = r2_settings()
        self.bucket = settings["bucket"]
        self.prefix = settings["prefix"]
        self.client = self._client(settings)

    def _client(self, settings: dict[str, str]):
        try:
            import boto3
        except ImportError as exc:
            raise AppError(
                "boto3 is required when DOCUMENT_STORAGE_BACKEND=r2.",
                code=ErrorCode.CONFIG_ERROR,
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=settings["endpoint_url"],
            aws_access_key_id=settings["access_key_id"],
            aws_secret_access_key=settings["secret_access_key"],
            region_name="auto",
        )

    def key_for(self, filename: str) -> str:
        return f"{self.prefix}{filename}"

    def exists(self, filename: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self.key_for(filename))
            return True
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str(response.get("Error", {}).get("Code", ""))
            status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status_code == 404:
                return False
            raise AppError("Could not check document storage.", code=ErrorCode.STORAGE_ERROR) from exc

    def save(self, filename: str, content: bytes) -> str:
        key = self.key_for(filename)
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=content)
        except Exception as exc:
            raise AppError("Could not upload document to R2.", code=ErrorCode.STORAGE_ERROR) from exc
        return f"r2://{self.bucket}/{key}"

    @contextmanager
    def document_files(self) -> Iterator[list[StoredDocumentFile]]:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            files = []
            for key in self._document_keys():
                destination = root / Path(key).name
                try:
                    self.client.download_file(self.bucket, key, str(destination))
                except Exception as exc:
                    raise AppError("Could not download document from R2.", code=ErrorCode.STORAGE_ERROR) from exc
                files.append(StoredDocumentFile(name=Path(key).name, path=destination))
            yield sorted(files, key=lambda item: item.name)

    def _document_keys(self) -> list[str]:
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        try:
            pages = paginator.paginate(Bucket=self.bucket, Prefix=self.prefix)
            for page in pages:
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if Path(key).suffix.lower() in SUPPORTED_STORAGE_EXTENSIONS:
                        keys.append(key)
        except Exception as exc:
            raise AppError("Could not list documents in R2.", code=ErrorCode.STORAGE_ERROR) from exc
        return keys


def document_storage() -> LocalDocumentStorage | R2DocumentStorage:
    backend = document_storage_backend()
    if backend == "local":
        return LocalDocumentStorage()
    if backend == "r2":
        return R2DocumentStorage()
    raise AppError(
        "DOCUMENT_STORAGE_BACKEND must be either local or r2.",
        code=ErrorCode.CONFIG_ERROR,
        details={"backend": backend},
    )
