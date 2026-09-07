import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from errors import AppError, ErrorCode
from services.document_storage import R2DocumentStorage, StoredDocumentFile, document_storage
from settings import DOCS_DIR


class RagLabStorage:
    """Private object storage for unpublished RAG Lab artifacts."""

    def __init__(self, storage=None) -> None:
        self.storage = storage or document_storage()
        self.local_root = DOCS_DIR.parent / "rag-lab"

    def file_key(self, experiment_id: str, file_id: str, filename: str) -> str:
        return f"rag-lab/{experiment_id}/files/{file_id}/{filename}"

    def extraction_key(self, experiment_id: str, file_id: str) -> str:
        return f"rag-lab/{experiment_id}/extractions/{file_id}.json"

    def save_bytes(self, key: str, content: bytes, content_type: str | None = None) -> str:
        if isinstance(self.storage, R2DocumentStorage):
            try:
                self.storage.client.put_object(
                    Bucket=self.storage.bucket,
                    Key=key,
                    Body=content,
                    ContentType=content_type or "application/octet-stream",
                )
            except Exception as exc:
                raise AppError("Could not store RAG Lab artifact.", code=ErrorCode.STORAGE_ERROR) from exc
            return f"r2://{self.storage.bucket}/{key}"

        destination = self.local_root / key.removeprefix("rag-lab/")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return str(destination)

    def save_json(self, key: str, value: dict) -> str:
        return self.save_bytes(key, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json")

    def load_json(self, key: str) -> dict:
        return json.loads(self._read_bytes(key).decode("utf-8"))

    @contextmanager
    def document_file(self, key: str, filename: str):
        if isinstance(self.storage, R2DocumentStorage):
            with TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / filename
                try:
                    self.storage.client.download_file(self.storage.bucket, key, str(destination))
                except Exception as exc:
                    raise AppError("Could not download RAG Lab file.", code=ErrorCode.STORAGE_ERROR) from exc
                yield StoredDocumentFile(name=filename, path=destination)
            return

        path = self.local_root / key.removeprefix("rag-lab/")
        if not path.exists():
            raise AppError("RAG Lab file was not found.", code=ErrorCode.NOT_FOUND)
        yield StoredDocumentFile(name=filename, path=path)

    def _read_bytes(self, key: str) -> bytes:
        if isinstance(self.storage, R2DocumentStorage):
            try:
                response = self.storage.client.get_object(Bucket=self.storage.bucket, Key=key)
                return response["Body"].read()
            except Exception as exc:
                raise AppError("Could not read RAG Lab artifact.", code=ErrorCode.STORAGE_ERROR) from exc
        path = self.local_root / key.removeprefix("rag-lab/")
        if not path.exists():
            raise AppError("RAG Lab artifact was not found.", code=ErrorCode.NOT_FOUND)
        return path.read_bytes()


def rag_lab_storage() -> RagLabStorage:
    return RagLabStorage()
