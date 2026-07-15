import json
import logging
import sys
from io import BytesIO
from types import SimpleNamespace

import pytest
from docx import Document
from fastapi import HTTPException, UploadFile, status
from starlette.datastructures import Headers

import ingest_service
import prompts
import retrieval
import routers.documents
import task_queue
from auth import validate_password
from chunking import chunk_document
from documents import extract_document_metadata, infer_title, read_docx, read_pdf_with_pdfplumber, remove_repeated_margin_lines
from errors import AppError
from rag_errors import RagError
from repositories import ChatRepository
from services.document_storage import R2DocumentStorage
from services.gemini import GeminiClient
from settings import validate_runtime_config
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


def test_prepare_uploads_rejects_aggregate_batch_over_memory_limit(monkeypatch):
    monkeypatch.setenv("ALLOWED_UPLOAD_EXTENSIONS", "txt")
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "10")
    monkeypatch.setenv("UPLOAD_BATCH_MAX_BYTES", "10")

    with pytest.raises(HTTPException) as exc:
        routers.documents.prepare_uploads(
            [
                upload_file("one.txt", b"123456"),
                upload_file("two.txt", b"123456"),
            ]
        )

    assert exc.value.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE


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


def test_ingest_queue_requires_configured_broker(monkeypatch):
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    with pytest.raises(AppError) as exc:
        task_queue.ensure_queue_configured()

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert exc.value.details == {"missing": ["CELERY_BROKER_URL"]}


def test_celery_broker_url_defaults_rediss_certificate_validation(monkeypatch):
    monkeypatch.setenv("CELERY_BROKER_URL", "rediss://default:secret@redis.example.com:6379/0")

    assert task_queue.celery_broker_url() == "rediss://default:secret@redis.example.com:6379/0?ssl_cert_reqs=required"


def test_enqueue_ingest_job_delegates_to_celery_task(monkeypatch):
    calls = []

    class Task:
        def apply_async(self, args, task_id):
            calls.append({"args": args, "task_id": task_id})
            return SimpleNamespace(id=task_id)

    monkeypatch.setenv("CELERY_BROKER_URL", "rediss://default:secret@redis.example.com:6379/0")
    monkeypatch.setitem(sys.modules, "tasks", SimpleNamespace(run_ingest_job_task=Task()))

    task_id = task_queue.enqueue_ingest_job("job-1", on_enqueued=lambda queued_task_id: calls.append({"queued": queued_task_id}))

    assert calls[0]["queued"] == task_id
    assert task_id == calls[1]["task_id"]
    assert calls[1]["args"] == ["job-1"]


def test_worker_status_reports_online_celery_consumer(monkeypatch):
    class Control:
        def ping(self, timeout):
            assert timeout == 2
            return [{"celery@worker-1": {"ok": "pong"}}]

    monkeypatch.setenv("CELERY_BROKER_URL", "redis://redis.example.com:6379/0")
    monkeypatch.setitem(sys.modules, "tasks", SimpleNamespace(celery_app=SimpleNamespace(control=Control())))

    assert task_queue.celery_worker_status() == {
        "status": "ok",
        "broker_configured": True,
        "broker_reachable": True,
        "workers_online": 1,
    }


def test_worker_availability_fails_before_job_is_queued(monkeypatch):
    class Control:
        def ping(self, timeout):
            return []

    monkeypatch.setenv("CELERY_BROKER_URL", "redis://redis.example.com:6379/0")
    monkeypatch.setenv("CELERY_REQUIRE_WORKER_ONLINE", "true")
    monkeypatch.setitem(sys.modules, "tasks", SimpleNamespace(celery_app=SimpleNamespace(control=Control())))

    with pytest.raises(AppError) as exc:
        task_queue.ensure_worker_available()

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert exc.value.message == "No ingestion worker is online."
    assert exc.value.details["workers_online"] == 0


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
            "match_count": 40,
        }
    ]
    assert contexts[0]["base_score"] == 0.82
    assert contexts[0]["score"] > 0.5


def test_reranker_boosts_exact_legal_identifier():
    candidates = [
        retrieval.context_from_row(
            {
                "id": "semantic",
                "document_id": "doc-1",
                "source": "general-guidance.pdf",
                "chunk_index": 0,
                "chunk_type": "section",
                "section_heading": "Transit permits",
                "page_start": 1,
                "page_end": 1,
                "content": "General guidance about transit permits.",
                "metadata": {},
                "similarity": 0.8,
            }
        ),
        retrieval.context_from_row(
            {
                "id": "exact",
                "document_id": "doc-2",
                "source": "rules-2022.pdf",
                "chunk_index": 4,
                "chunk_type": "section",
                "section_heading": "Rule 12",
                "page_start": 5,
                "page_end": 5,
                "content": "Rule 12 requires prior approval for the transit permit.",
                "metadata": {"identifiers": ["Rule 12"], "years": ["2022"]},
                "similarity": 0.68,
            }
        ),
    ]

    ranked = retrieval.rerank_candidates("What does Rule 12 require?", candidates)

    assert ranked[0]["id"] == "exact"
    assert ranked[0]["metadata"]["retrieval"]["identifier_match"] == 1.0


def test_reranker_keeps_hybrid_score_dominant_for_generic_matches():
    candidates = [
        retrieval.context_from_row(
            {
                "id": "strong-hybrid",
                "document_id": "doc-1",
                "source": "forest-guidelines.pdf",
                "chunk_index": 0,
                "chunk_type": "section",
                "section_heading": "Forest diversion",
                "page_start": 1,
                "page_end": 1,
                "content": "Forest diversion proposals require scrutiny by the competent authority.",
                "metadata": {},
                "similarity": 0.84,
            },
            rank=0,
        ),
        retrieval.context_from_row(
            {
                "id": "weak-lexical",
                "document_id": "doc-2",
                "source": "general-note.pdf",
                "chunk_index": 0,
                "chunk_type": "section",
                "section_heading": "Forest diversion proposal scrutiny",
                "page_start": 5,
                "page_end": 5,
                "content": "This paragraph repeats forest diversion proposal scrutiny terms but is generic.",
                "metadata": {},
                "similarity": 0.62,
            },
            rank=1,
        ),
    ]

    ranked = retrieval.rerank_candidates("forest diversion proposal scrutiny", candidates)

    assert ranked[0]["id"] == "strong-hybrid"


def test_retrieve_expands_neighbor_chunks(monkeypatch):
    class Repository:
        def match_chunks(self, _query_embedding, _query_text, _match_count):
            return [
                {
                    "id": "chunk-2",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": 2,
                    "chunk_type": "section",
                    "section_heading": "Rule 12",
                    "page_start": 2,
                    "page_end": 2,
                    "content": "Rule 12 requires prior approval.",
                    "metadata": {"identifiers": ["Rule 12"]},
                    "similarity": 0.9,
                }
            ]

        def neighbor_chunks(self, _document_id, _chunk_index, radius=1):
            assert radius == 1
            return [
                {
                    "id": "chunk-2",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": 2,
                    "chunk_type": "section",
                    "section_heading": "Rule 12",
                    "page_start": 2,
                    "page_end": 2,
                    "content": "Rule 12 requires prior approval.",
                    "metadata": {},
                },
                {
                    "id": "chunk-3",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": 3,
                    "chunk_type": "section",
                    "section_heading": "Rule 12",
                    "page_start": 3,
                    "page_end": 3,
                    "content": "The exception applies to emergency work.",
                    "metadata": {},
                },
            ]

    monkeypatch.setattr(retrieval, "embed_query", lambda _text: [0.1])

    contexts = retrieval.retrieve("What does Rule 12 require?", top_k=2, repository=Repository())

    assert [context["id"] for context in contexts] == ["chunk-2", "chunk-3"]
    assert contexts[1]["evidence_role"] == "neighbor"


def test_neighbor_expansion_does_not_displace_direct_matches(monkeypatch):
    class Repository:
        def match_chunks(self, _query_embedding, _query_text, _match_count):
            return [
                {
                    "id": f"chunk-{index}",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": index,
                    "chunk_type": "section",
                    "section_heading": f"Rule {index}",
                    "page_start": index,
                    "page_end": index,
                    "content": f"Directly matched rule text {index}.",
                    "metadata": {},
                    "similarity": 0.9 - (index * 0.01),
                }
                for index in range(3)
            ]

        def neighbor_chunks(self, _document_id, _chunk_index, radius=1):
            return [
                {
                    "id": "neighbor",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": 99,
                    "chunk_type": "section",
                    "section_heading": "Nearby",
                    "page_start": 99,
                    "page_end": 99,
                    "content": "Nearby context.",
                    "metadata": {},
                }
            ]

    monkeypatch.setattr(retrieval, "embed_query", lambda _text: [0.1])

    contexts = retrieval.retrieve("rule text", top_k=3, repository=Repository())

    assert [context["id"] for context in contexts] == ["chunk-0", "chunk-1", "chunk-2"]
    assert all(context["evidence_role"] == "matched" for context in contexts)


def test_retrieve_keeps_low_scored_hybrid_candidates_by_default(monkeypatch):
    class Repository:
        def match_chunks(self, _query_embedding, _query_text, _match_count):
            return [
                {
                    "id": "chunk-low",
                    "document_id": "doc-1",
                    "source": "rules.pdf",
                    "chunk_index": 0,
                    "chunk_type": "section",
                    "section_heading": "Prior approval",
                    "page_start": 1,
                    "page_end": 1,
                    "content": "Prior approval is required for diversion of forest land.",
                    "metadata": {},
                    "similarity": 0.08,
                }
            ]

    monkeypatch.setattr(retrieval, "embed_query", lambda _text: [0.1])
    monkeypatch.delenv("RETRIEVAL_MIN_CONTEXT_SCORE", raising=False)
    monkeypatch.delenv("RETRIEVAL_CONFIDENCE_THRESHOLD", raising=False)

    contexts = retrieval.retrieve("Is prior approval required?", top_k=1, repository=Repository())

    assert len(contexts) == 1
    assert retrieval.retrieval_is_confident(contexts)


def test_chunk_embedding_includes_document_and_section_context():
    text = retrieval.embedding_text(
        {
            "source": "rules.pdf",
            "section_heading": "Rule 12 Prior approval",
            "content": "Approval is required.",
            "metadata": {
                "title": "Forest Conservation Rules, 2022",
                "document_type": "rules",
                "authority": "Ministry of Environment",
            },
        }
    )

    assert "Document: Forest Conservation Rules, 2022" in text
    assert "Section: Rule 12 Prior approval" in text
    assert "Legal identifiers: Rule 12" in text


def test_document_metadata_extracts_legal_fields_and_better_title():
    pages = [
        {
            "page": 1,
            "text": (
                "EXTRAORDINARY\n"
                "MINISTRY OF ENVIRONMENT, FOREST AND CLIMATE CHANGE\n"
                "THE FOREST CONSERVATION RULES, 2022\n"
                "G.S.R. 480(E). Rule 12 requires approval."
            ),
        }
    ]

    title = infer_title("gazette.pdf", pages)
    metadata = extract_document_metadata("gazette.pdf", title, pages)

    assert title == "THE FOREST CONSERVATION RULES, 2022"
    assert metadata["document_type"] == "rules"
    assert "G.S.R. 480(E)" in metadata["identifiers"]
    assert metadata["years"] == ["2022"]


def test_repeated_pdf_margin_lines_are_removed():
    pages = [
        {"page": number, "text": f"REPEATED HEADER\nPage-specific text {number}\nREPEATED FOOTER"}
        for number in range(1, 5)
    ]

    cleaned = remove_repeated_margin_lines(pages)

    assert all("REPEATED HEADER" not in page["text"] for page in cleaned)
    assert all("Page-specific text" in page["text"] for page in cleaned)


def test_answer_abstains_when_retrieval_confidence_is_low(monkeypatch):
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda _prompt: pytest.fail("model should not be called"))

    answer = prompts.answer_with_gemini("Question?", [])

    assert answer == prompts.INSUFFICIENT_EVIDENCE_ANSWER
    assert prompts.answer_is_abstention(answer)


def test_answer_uses_model_when_low_scored_context_exists(monkeypatch):
    called = []
    monkeypatch.setattr(prompts, "generate_with_gemini", lambda _prompt: called.append(True) or "Approval is required [1].")
    monkeypatch.delenv("RETRIEVAL_CONFIDENCE_THRESHOLD", raising=False)

    answer = prompts.answer_with_gemini(
        "Is approval required?",
        [
            {
                "source": "rules.pdf",
                "chunk_index": 0,
                "section_heading": "Prior approval",
                "page_start": 1,
                "page_end": 1,
                "text": "Approval is required.",
                "score": 0.04,
                "base_score": 0.08,
                "evidence_role": "matched",
            }
        ],
    )

    assert called == [True]
    assert answer == "Approval is required [1]."


def test_citation_validation_preserves_uncited_answers_and_removes_invalid_references():
    assert prompts.validate_answer_citations("Approval is required.", 2) == "Approval is required."
    assert prompts.validate_answer_citations("Approval is required [1], not [8].", 2) == "Approval is required [1], not ."


def test_unsupported_answer_is_not_reported_as_abstention():
    assert not prompts.answer_is_abstention(prompts.UNSUPPORTED_ANSWER)


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
    closed = []

    class Pdf:
        pages = [
            SimpleNamespace(
                extract_text=lambda: "Schedule of transit fees",
                extract_tables=lambda: [[["Species", "Unit", "Fee"], ["Teak", "Cubic meter", "1200"]]],
                close=lambda: closed.append(True),
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
    assert closed == [True]


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


def test_runtime_config_allows_zero_retrieval_max_per_source(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("DOCUMENT_STORAGE_BACKEND", "local")
    monkeypatch.setenv("RETRIEVAL_MAX_PER_SOURCE", "0")
    monkeypatch.setenv("CELERY_BROKER_URL", "rediss://default:secret@redis.example.com:6379/0")

    status = validate_runtime_config()

    assert status["ok"] is True
    assert "RETRIEVAL_MAX_PER_SOURCE" not in status["invalid"]


def test_runtime_config_rejects_negative_retrieval_max_per_source(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("SUPABASE_URL", "https://project-ref.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
    monkeypatch.setenv("JWT_SECRET_KEY", "test-jwt-secret")
    monkeypatch.setenv("DOCUMENT_STORAGE_BACKEND", "local")
    monkeypatch.setenv("RETRIEVAL_MAX_PER_SOURCE", "-1")

    status = validate_runtime_config()

    assert status["ok"] is False
    assert "RETRIEVAL_MAX_PER_SOURCE" in status["invalid"]


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

        def delete_chunks(self, _source):
            pass

        def insert_chunk_batch(self, _rows):
            raise RuntimeError("insert failed")

        def mark_document_status(self, _source, status, _details=None):
            self.statuses.append(status)

    repository = Repository()
    monkeypatch.setattr(
        ingest_service,
        "iter_documents",
        lambda source=None: iter([{"source": "a.txt", "kind": "txt", "title": "A", "page_count": None, "pages": []}]),
    )
    monkeypatch.setattr(ingest_service, "iter_document_chunks", lambda _doc: iter([{"source": "a.txt", "content": "x"}]))
    monkeypatch.setattr(ingest_service, "chunk_row", lambda _document_id, _chunk: {"source": "a.txt"})

    with pytest.raises(RuntimeError):
        ingest_service.build_index(repository)

    assert repository.statuses == ["indexing", "failed"]


def test_ingest_persists_embedding_rows_in_bounded_batches(monkeypatch):
    class Repository:
        def __init__(self):
            self.batches = []

        def indexed_sources(self):
            return set()

        def upsert_document(self, _doc, status="indexing"):
            return "document-id"

        def delete_chunks(self, _source):
            pass

        def insert_chunk_batch(self, rows):
            self.batches.append(list(rows))
            return len(rows)

        def mark_document_status(self, _source, _status, _details=None):
            pass

    repository = Repository()
    monkeypatch.setenv("INGEST_BATCH_SIZE", "2")
    monkeypatch.setenv("MAX_DOCUMENT_CHUNKS", "10")
    monkeypatch.setattr(
        ingest_service,
        "iter_documents",
        lambda source=None: iter([{"source": source, "kind": "txt", "title": "A", "page_count": None, "pages": []}]),
    )
    monkeypatch.setattr(
        ingest_service,
        "iter_document_chunks",
        lambda _doc: iter({"source": "a.txt", "content": str(index)} for index in range(5)),
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_row",
        lambda _document_id, chunk: {"source": chunk["source"], "content": chunk["content"]},
    )

    result = ingest_service.build_index(repository, source="a.txt")

    assert [len(batch) for batch in repository.batches] == [2, 2, 1]
    assert result["chunks_added"] == 5
    assert result["source"] == "a.txt"


def test_ingest_job_processes_only_source_stored_in_job_metadata(monkeypatch):
    updates = []
    calls = []

    class JobRepository:
        def get(self, _job_id):
            return {"id": "job-1", "metadata": {"source": "rules.pdf", "scope": "document"}}

        def update(self, job_id, **values):
            updates.append((job_id, values))

    monkeypatch.setattr(
        ingest_service,
        "build_index",
        lambda *, source=None: calls.append(source) or {"source": source, "chunks_added": 3},
    )

    ingest_service.run_ingest_job("job-1", repository=JobRepository(), raise_on_failure=True)

    assert calls == ["rules.pdf"]
    assert [values["status"] for _job_id, values in updates] == ["running", "succeeded"]


def test_preview_chunks_returns_bounded_page_and_omits_content_by_default(monkeypatch):
    monkeypatch.setattr(
        ingest_service,
        "iter_documents",
        lambda source=None: iter([{"source": "a.txt", "kind": "txt", "title": "A", "pages": []}]),
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_document",
        lambda _doc, max_chunks=None: [
            {
                "source": "a.txt",
                "chunk_index": index,
                "content": f"chunk {index}",
                "metadata": {},
            }
            for index in range(max_chunks or 0)
        ],
    )

    result = ingest_service.preview_chunks(source="a.txt", limit=2)

    assert result["chunks_returned"] == 2
    assert result["has_more"] is True
    assert result["chunks"][0]["content"] == ""
    assert result["chunks"][0]["content_omitted"] is True
    assert result["chunks"][0]["content_chars"] == len("chunk 0")


def test_preview_chunks_can_include_truncated_content(monkeypatch):
    monkeypatch.setattr(
        ingest_service,
        "iter_documents",
        lambda source=None: iter([{"source": "a.txt", "kind": "txt", "title": "A", "pages": []}]),
    )
    monkeypatch.setattr(
        ingest_service,
        "chunk_document",
        lambda _doc, max_chunks=None: [
            {
                "source": "a.txt",
                "chunk_index": 0,
                "content": "abcdef",
                "metadata": {},
            }
        ],
    )

    result = ingest_service.preview_chunks(source="a.txt", limit=1, include_content=True, max_content_chars=4)

    assert result["chunks"][0]["content"] == "abcd"
    assert result["chunks"][0]["content_truncated"] is True
    assert result["chunks"][0]["content_chars"] == 6


def test_preview_chunks_passes_source_filter_to_document_iterator(monkeypatch):
    seen_sources = []

    def fake_iter_documents(source=None):
        seen_sources.append(source)
        return iter([])

    monkeypatch.setattr(ingest_service, "iter_documents", fake_iter_documents)

    result = ingest_service.preview_chunks(source="rules.pdf", limit=10)

    assert result["chunks"] == []
    assert result["source"] == "rules.pdf"
    assert result["all_sources"] is False
    assert seen_sources == ["rules.pdf"]


def test_preview_chunks_requires_source_unless_all_sources_is_explicit():
    with pytest.raises(AppError) as exc_info:
        ingest_service.preview_chunks(limit=10)

    assert "requires a source filename" in exc_info.value.message


def test_preview_chunks_all_sources_requires_explicit_opt_in(monkeypatch):
    seen_sources = []

    def fake_iter_documents(source=None):
        seen_sources.append(source)
        return iter([])

    monkeypatch.setattr(ingest_service, "iter_documents", fake_iter_documents)

    result = ingest_service.preview_chunks(all_sources=True, limit=10)

    assert result["chunks"] == []
    assert result["source"] is None
    assert result["all_sources"] is True
    assert seen_sources == [None]


def test_preview_chunks_rejects_source_and_all_sources_together():
    with pytest.raises(AppError) as exc_info:
        ingest_service.preview_chunks(source="rules.pdf", all_sources=True)

    assert "Use either source or all_sources" in exc_info.value.message
