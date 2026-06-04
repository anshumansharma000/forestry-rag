import json
import logging
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import UploadFile, status
from starlette.datastructures import Headers

import ingest_service
import retrieval
import routers.documents
from auth import validate_password
from chunking import chunk_document
from documents import read_docx, read_pdf_with_pdfplumber
from errors import AppError
from rag_errors import RagError
from repositories import ChatRepository
from services.document_storage import R2DocumentStorage
from services.gemini import GeminiClient
from structured_logging import JsonLogFormatter
from upload_utils import allowed_upload_extensions, safe_filename


def upload_file(filename: str, content: bytes = b"content", content_type: str = "text/plain") -> UploadFile:
    return UploadFile(
        BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


def test_password_validation_rejects_weak_passwords():
    with pytest.raises(RagError):
        validate_password("short1")

    with pytest.raises(RagError):
        validate_password("longbutnodigits")


def test_json_log_formatter_preserves_structured_context():
    record = logging.LogRecord(
        name="tests.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="user_created",
        args=(),
        exc_info=None,
    )
    record.actor_user_id = "user-1"
    record.duration_ms = 12.5

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["level"] == "info"
    assert payload["logger"] == "tests.logger"
    assert payload["message"] == "user_created"
    assert payload["actor_user_id"] == "user-1"
    assert payload["duration_ms"] == 12.5
    assert "timestamp" in payload


def test_upload_helpers_normalize_names_and_extensions(monkeypatch):
    monkeypatch.setenv("ALLOWED_UPLOAD_EXTENSIONS", "pdf, txt, docx")

    assert safe_filename("../Forest Rules?.pdf") == "Forest Rules_.pdf"
    assert allowed_upload_extensions() == {"pdf", "txt", "docx"}


def test_prepare_uploads_accepts_multiple_files(monkeypatch):
    monkeypatch.setenv("ALLOWED_UPLOAD_EXTENSIONS", "pdf, txt, docx")

    pending = routers.documents.prepare_uploads(
        [
            upload_file("Forest Rules.pdf", b"pdf-bytes", "application/pdf"),
            upload_file("Notes.txt", b"text-bytes"),
        ]
    )

    assert [(item.filename, item.content, item.content_type) for item in pending] == [
        ("Forest Rules.pdf", b"pdf-bytes", "application/pdf"),
        ("Notes.txt", b"text-bytes", "text/plain"),
    ]


def test_prepare_uploads_rejects_duplicate_filenames_in_one_request(monkeypatch):
    monkeypatch.setenv("ALLOWED_UPLOAD_EXTENSIONS", "pdf, txt, docx")

    with pytest.raises(AppError) as exc:
        routers.documents.prepare_uploads(
            [
                upload_file("Forest Rules.pdf"),
                upload_file("Forest Rules.pdf"),
            ]
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert exc.value.details == {"filename": "Forest Rules.pdf"}


def test_batch_save_checks_conflicts_before_writing(monkeypatch):
    saved = []

    class Storage:
        def exists(self, filename):
            return filename == "existing.pdf"

        def save(self, filename, content):
            saved.append((filename, content))
            return f"/docs/{filename}"

    monkeypatch.delenv("ALLOW_DOCUMENT_REPLACE", raising=False)
    monkeypatch.setattr(routers.documents, "document_storage", lambda: Storage())
    monkeypatch.setattr(routers.documents, "audit_event", lambda *_args, **_kwargs: None)

    with pytest.raises(AppError) as exc:
        routers.documents.save_uploads(
            SimpleNamespace(client=None, headers={}),
            SimpleNamespace(id="user-1", email="user@example.com", role="knowledge_manager"),
            [
                routers.documents.PendingUpload("new.pdf", b"new", "application/pdf"),
                routers.documents.PendingUpload("existing.pdf", b"existing", "application/pdf"),
            ],
        )

    assert exc.value.status_code == status.HTTP_409_CONFLICT
    assert saved == []


def test_r2_document_storage_uses_prefixed_s3_keys(monkeypatch):
    calls = []

    class Client:
        def put_object(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setenv("R2_ACCOUNT_ID", "account-id")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "access-key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-key")
    monkeypatch.setenv("R2_BUCKET", "fisrag-docs")
    monkeypatch.setenv("R2_PREFIX", "source-docs")
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        SimpleNamespace(client=lambda *_args, **_kwargs: Client()),
    )

    path = R2DocumentStorage().save("rules.pdf", b"content")

    assert path == "r2://fisrag-docs/source-docs/rules.pdf"
    assert calls == [{"Bucket": "fisrag-docs", "Key": "source-docs/rules.pdf", "Body": b"content"}]


def test_create_presigned_uploads_uses_isolated_staging_key(monkeypatch):
    calls = []

    class Storage:
        backend = "r2"
        prefix = "docs/"

        def exists(self, filename):
            return False

        def key_for(self, filename):
            return f"docs/{filename}"

        def presigned_put_url(self, key, content_type, expires_in_seconds):
            calls.append({"key": key, "content_type": content_type, "expires_in_seconds": expires_in_seconds})
            return f"https://r2.example/{key}"

        def head_key(self, key):
            return None

        def copy_key(self, source_key, destination_key):
            return f"r2://bucket/{destination_key}"

        def delete_key(self, key):
            pass

    monkeypatch.setattr(routers.documents, "document_storage", lambda: Storage())
    monkeypatch.setenv("R2_UPLOAD_STAGING_PREFIX", "pending")
    monkeypatch.setenv("PRESIGNED_UPLOAD_EXPIRES_SECONDS", "600")

    [upload] = routers.documents.create_presigned_uploads(
        SimpleNamespace(id="user-1"),
        [routers.documents.PendingDirectUpload("rules.pdf", 7, "application/pdf")],
    )

    assert upload["filename"] == "rules.pdf"
    assert upload["method"] == "PUT"
    assert upload["headers"] == {"Content-Type": "application/pdf"}
    assert upload["expires_in_seconds"] == 600
    assert calls[0]["key"].startswith(f"pending/user-1/{upload['upload_id']}/")
    assert calls[0]["key"].endswith("/rules.pdf")
    assert calls[0]["content_type"] == "application/pdf"


def test_complete_direct_uploads_copies_staged_file_and_audits(monkeypatch):
    calls = []
    audits = []

    class Storage:
        backend = "r2"
        prefix = "docs/"

        def exists(self, filename):
            return False

        def key_for(self, filename):
            return f"docs/{filename}"

        def presigned_put_url(self, key, content_type, expires_in_seconds):
            return f"https://r2.example/{key}"

        def head_key(self, key):
            calls.append(("head", key))
            return {"ContentLength": 12, "ContentType": "application/pdf"}

        def copy_key(self, source_key, destination_key):
            calls.append(("copy", source_key, destination_key))
            return f"r2://bucket/{destination_key}"

        def delete_key(self, key):
            calls.append(("delete", key))

    upload_id = "3792898e-4aef-4985-a786-2980c069098f"
    monkeypatch.setattr(routers.documents, "document_storage", lambda: Storage())
    monkeypatch.setattr(routers.documents, "audit_event", lambda *args, **_kwargs: audits.append(args))
    monkeypatch.setenv("R2_UPLOAD_STAGING_PREFIX", "pending")

    [result] = routers.documents.complete_direct_uploads(
        SimpleNamespace(client=None, headers={}),
        SimpleNamespace(id="user-1", email="user@example.com", role="knowledge_manager"),
        [routers.documents.CompletedDirectUpload(upload_id, "rules.pdf")],
    )

    staging_key = f"pending/user-1/{upload_id}/rules.pdf"
    assert result == {"status": "ok", "filename": "rules.pdf", "path": "r2://bucket/docs/rules.pdf"}
    assert calls == [("head", staging_key), ("copy", staging_key, "docs/rules.pdf"), ("delete", staging_key)]
    assert audits[0][2] == "documents.upload"
    assert audits[0][5]["direct_upload"] is True
    assert audits[0][5]["bytes"] == 12


def test_chunk_document_preserves_heading_context(monkeypatch):
    monkeypatch.setenv("CHUNK_TOKENS", "80")
    doc = {
        "source": "rules.txt",
        "kind": "txt",
        "title": "Forest Rules",
        "pages": [
            {
                "page": 1,
                "text": "Section 1 Introduction\n\n1. This rule applies to forest transit permits. It has clear conditions.",
            }
        ],
    }

    chunks = chunk_document(doc)

    assert chunks
    assert chunks[0]["source"] == "rules.txt"
    assert "forest transit permits" in chunks[0]["content"]


def test_retrieve_passes_query_text_for_hybrid_search(monkeypatch):
    calls = []

    class Repository:
        def match_chunks(self, query_embedding, query_text, match_count):
            calls.append(
                {
                    "query_embedding": query_embedding,
                    "query_text": query_text,
                    "match_count": match_count,
                }
            )
            return [
                {
                    "id": "chunk-1",
                    "document_id": "doc-1",
                    "source": "forest-rules.pdf",
                    "chunk_index": 0,
                    "chunk_type": "section",
                    "section_heading": "Transit permits",
                    "page_start": 1,
                    "page_end": 1,
                    "content": "Transit permits require approval.",
                    "metadata": {},
                    "similarity": 0.82,
                }
            ]

    monkeypatch.setattr(retrieval, "embed_query", lambda text: [0.1, 0.2, 0.3])

    contexts = retrieval.retrieve("Rule 12 transit permits", top_k=7, repository=Repository())

    assert calls == [
        {
            "query_embedding": [0.1, 0.2, 0.3],
            "query_text": "Rule 12 transit permits",
            "match_count": 7,
        }
    ]
    assert contexts[0]["score"] == 0.82


def test_read_docx_extracts_tables_as_structured_blocks(tmp_path):
    path = tmp_path / "fees.docx"
    document = Document()
    document.add_paragraph("Schedule of transit fees")
    table = document.add_table(rows=3, cols=3)
    table.rows[0].cells[0].text = "Species"
    table.rows[0].cells[1].text = "Unit"
    table.rows[0].cells[2].text = "Fee"
    table.rows[1].cells[0].text = "Teak"
    table.rows[1].cells[1].text = "Cubic meter"
    table.rows[1].cells[2].text = "1200"
    table.rows[2].cells[0].text = "Bamboo"
    table.rows[2].cells[1].text = "Bundle"
    table.rows[2].cells[2].text = "50"
    document.save(path)

    pages = read_docx(path)

    assert pages[0]["blocks"][0] == {"type": "text", "text": "Schedule of transit fees"}
    assert pages[0]["blocks"][1]["type"] == "table"
    assert pages[0]["blocks"][1]["headers"] == ["Species", "Unit", "Fee"]
    assert pages[0]["blocks"][1]["rows"][0] == ["Teak", "Cubic meter", "1200"]


def test_read_pdf_extracts_tables_as_structured_blocks(monkeypatch, tmp_path):
    class Pdf:
        pages = [
            SimpleNamespace(
                extract_text=lambda: "Schedule of transit fees",
                extract_tables=lambda: [[["Species", "Unit", "Fee"], ["Teak", "Cubic meter", "1200"]]],
            )
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setitem(sys.modules, "pdfplumber", SimpleNamespace(open=lambda _path: Pdf()))

    pages = read_pdf_with_pdfplumber(tmp_path / "fees.pdf")

    assert pages[0]["page"] == 1
    assert pages[0]["blocks"][0] == {"type": "text", "text": "Schedule of transit fees"}
    assert pages[0]["blocks"][1]["type"] == "table"
    assert pages[0]["blocks"][1]["headers"] == ["Species", "Unit", "Fee"]
    assert pages[0]["blocks"][1]["rows"] == [["Teak", "Cubic meter", "1200"]]


def test_chunk_document_preserves_table_header_context():
    doc = {
        "source": "fees.docx",
        "kind": "docx",
        "title": "Transit Fees",
        "pages": [
            {
                "page": None,
                "text": "Schedule 1 Transit Fees\n\nSpecies | Unit | Fee\nTeak | Cubic meter | 1200",
                "blocks": [
                    {"type": "text", "text": "Schedule 1 Transit Fees"},
                    {
                        "type": "table",
                        "table_index": 0,
                        "headers": ["Species", "Unit", "Fee"],
                        "rows": [["Teak", "Cubic meter", "1200"], ["Bamboo", "Bundle", "50"]],
                        "text": "Species | Unit | Fee\nTeak | Cubic meter | 1200\nBamboo | Bundle | 50",
                    },
                ],
            }
        ],
    }

    chunks = chunk_document(doc, max_tokens=120, overlap_tokens=0)
    table_chunks = [chunk for chunk in chunks if chunk["chunk_type"] == "table"]

    assert table_chunks
    assert "Columns: Species, Unit, Fee" in table_chunks[0]["content"]
    assert "Species: Teak" in table_chunks[0]["content"]
    assert "Unit: Cubic meter" in table_chunks[0]["content"]
    assert table_chunks[0]["metadata"]["table_indexes"] == [0]


def test_gemini_client_redacts_upstream_error_body(monkeypatch):
    class Response:
        status_code = 500
        text = "secret upstream diagnostics"

        def json(self):
            return {}

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr("services.gemini.requests.post", lambda *args, **kwargs: Response())

    with pytest.raises(AppError) as exc:
        GeminiClient().generate("hello")

    assert exc.value.message == "Gemini API returned an error."
    assert exc.value.details == {"status_code": 500}
    assert "secret upstream diagnostics" not in exc.value.message


def test_chat_repository_rejects_missing_owned_session():
    class Query:
        data = []

        def select(self, *_args):
            return self

        def eq(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def execute(self):
            return self

    class Client:
        def table(self, _name):
            return Query()

    with pytest.raises(RagError):
        ChatRepository(Client()).assert_session_owner("session-id", "user-id")


def test_ingest_marks_document_failed_when_chunk_insert_fails(monkeypatch):
    class Repository:
        def __init__(self):
            self.statuses = []

        def indexed_sources(self):
            return set()

        def upsert_document(self, _doc, status="indexing"):
            self.statuses.append(status)
            return "document-id"

        def replace_chunks(self, _source, _rows):
            raise RuntimeError("insert failed")

        def mark_document_status(self, _source, status, _details=None):
            self.statuses.append(status)

    repository = Repository()
    monkeypatch.setattr(
        ingest_service,
        "load_documents",
        lambda: [{"source": "a.txt", "kind": "txt", "title": "A", "page_count": None, "pages": []}],
    )
    monkeypatch.setattr(ingest_service, "chunk_document", lambda _doc: [{"source": "a.txt", "content": "x"}])
    monkeypatch.setattr(ingest_service, "chunk_row", lambda _document_id, _chunk: {"source": "a.txt"})

    with pytest.raises(RuntimeError):
        ingest_service.build_index(repository)

    assert repository.statuses == ["indexing", "failed"]
