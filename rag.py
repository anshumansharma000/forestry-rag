# ruff: noqa: F401
"""Compatibility facade for RAG helpers.

The implementation lives in focused modules. Existing imports can keep using
`rag.py` while new code should depend on the narrower modules directly.
"""

from chat_service import (
    chat_ask,
    create_chat_session,
    delete_chat_message,
    delete_chat_session,
    get_chat_messages,
    list_chat_sessions,
    save_chat_message,
)
from chunking import (
    add_context_unit,
    chunk_document,
    chunk_settings,
    chunk_token_count,
    clean_heading,
    context_unit,
    count_tokens,
    document_profile,
    document_units,
    faq_document_units,
    fit_overlap,
    is_answer_start,
    is_boilerplate_line,
    is_clause_start,
    is_faq_question,
    is_heading,
    is_numbered_faq_line,
    overlap_units,
    pack_text_parts,
    page_units,
    split_long_text,
    split_sentences,
    split_subclauses,
    split_with_context,
    strip_faq_question_number,
    token_chunks,
    unit_heading,
    unit_pages,
    unit_types,
)
from documents import (
    DOCS_DIR,
    infer_title,
    load_documents,
    normalize_text,
    read_docx,
    read_pdf,
    read_txt,
)
from ingest_service import build_index, preview_chunks
from prompts import answer_with_gemini, format_history, rewrite_question_for_retrieval
from rag_errors import RagError
from repositories import DocumentRepository
from retrieval import (
    chunk_row,
    embed_query,
    embed_text,
    format_source,
    generate_with_gemini,
    normalize_embedding,
    retrieve,
    source_payload,
)
from services.storage import supabase_client
from settings import config_status, validate_runtime_config, validate_supabase_settings


def indexed_sources(client) -> set[str]:
    return DocumentRepository(client).indexed_sources()
