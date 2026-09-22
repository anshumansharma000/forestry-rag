from uuid import uuid4

from consistency import OperationBusy
from conversation_context import select_history, should_select_history
from errors import AppError, ErrorCode
from prompts import answer_is_abstention, answer_outcome, answer_with_gemini, rewrite_question_for_retrieval
from rag_errors import RagError
from repositories import ChatRepository, DocumentRepository
from retrieval import cited_source_payload, retrieval_confidence, retrieve, source_payload
from token_usage import track_query_usage


def create_chat_session(title: str | None = None, user_id: str | None = None, repository: ChatRepository | None = None) -> dict:
    repository = repository or ChatRepository()
    return repository.create_session(title, user_id)


def list_chat_sessions(user_id: str, limit: int = 20, repository: ChatRepository | None = None) -> list[dict]:
    repository = repository or ChatRepository()
    return repository.list_sessions(user_id, limit)


def get_chat_messages(
    session_id: str,
    user_id: str,
    limit: int | None = None,
    repository: ChatRepository | None = None,
    document_repository: DocumentRepository | None = None,
) -> list[dict]:
    repository = repository or ChatRepository()
    messages = repository.get_messages(session_id, user_id, limit)
    return enrich_legacy_citations(messages, document_repository=document_repository)


def enrich_legacy_citations(
    messages: list[dict], document_repository: DocumentRepository | None = None
) -> list[dict]:
    """Add trusted document IDs to resolvable citations written by older releases."""
    unresolved_sources = {
        source.get("source")
        for message in messages
        if message.get("role") == "assistant"
        for source in (message.get("sources") or [])
        if isinstance(source, dict) and not source.get("document_id") and source.get("source")
    }
    if not unresolved_sources:
        return messages

    try:
        ids_by_source = (document_repository or DocumentRepository()).document_ids_by_sources(unresolved_sources)
    except Exception:
        # Legacy rows that can no longer be resolved remain non-downloadable.
        return messages

    enriched = []
    for message in messages:
        copied = {**message}
        copied_sources = []
        for source in message.get("sources") or []:
            if not isinstance(source, dict):
                copied_sources.append(source)
                continue
            copied_source = {**source}
            resolved_id = ids_by_source.get(source.get("source"))
            if not copied_source.get("document_id") and resolved_id:
                copied_source["document_id"] = resolved_id
            copied_sources.append(copied_source)
        copied["sources"] = copied_sources
        enriched.append(copied)
    return enriched


def save_chat_message(
    session_id: str,
    role: str,
    content: str,
    sources: list[dict] | None = None,
    metadata: dict | None = None,
    repository: ChatRepository | None = None,
) -> dict:
    repository = repository or ChatRepository()
    return repository.save_message(session_id, role, content, sources, metadata)


def delete_chat_session(session_id: str, user_id: str, repository: ChatRepository | None = None) -> dict:
    repository = repository or ChatRepository()
    return {"deleted": True, "session": repository.delete_session(session_id, user_id)}


def delete_chat_message(session_id: str, message_id: str, user_id: str, repository: ChatRepository | None = None) -> dict:
    repository = repository or ChatRepository()
    return {"deleted": True, "message": repository.delete_message(session_id, message_id, user_id)}


@track_query_usage
def chat_ask(session_id: str, message: str, user_id: str, top_k: int | None = None,
             repository: ChatRepository | None = None, *, request_id: str | None = None) -> dict:
    if not message.strip():
        raise RagError("message is required")
    repository = repository or ChatRepository()
    request_id = request_id or str(uuid4())
    token = str(uuid4())
    claim = repository.begin_turn(session_id, user_id, request_id, {'message': message, 'top_k': top_k}, token)
    if claim['state'] == 'completed':
        return claim['response']
    if claim['state'] == 'not_found':
        raise AppError('Chat session not found.', code=ErrorCode.NOT_FOUND, status_code=404)
    if claim['state'] == 'gone':
        raise AppError('This chat turn was deleted.', code=ErrorCode.NOT_FOUND, status_code=410)
    if claim['state'] == 'conflict':
        raise AppError('Request ID was already used with different input.', code=ErrorCode.CONFLICT, status_code=409)
    if claim['state'] != 'claimed':
        raise OperationBusy()
    with repository.turn_lease(session_id, token) as lease:
        previous_messages = get_chat_messages(session_id, user_id, repository=repository)
        answer_history = previous_messages
        from jev_policy import enabled, standalone_question
        from jev_shadow import history_selection

        if standalone_question(previous_messages, message):
            search_query = message.strip()
        elif enabled("history") == "active":
            # Query resolution is generative work owned by Gemini. History selection
            # is a separate bounded classification task owned exclusively by Jev.
            search_query = rewrite_question_for_retrieval(previous_messages, message)
        elif should_select_history(previous_messages):
            search_query, answer_history = select_history(previous_messages, message)
        else:
            search_query = rewrite_question_for_retrieval(previous_messages, message)
        answer_history = history_selection(previous_messages, message, answer_history)
        contexts = retrieve(search_query, top_k)
        answer = answer_with_gemini(message, contexts, answer_history)
        result = {
            'session_id': session_id, 'request_id': request_id, 'search_query': search_query,
            'answer': answer, 'outcome': answer_outcome(answer), 'sources': source_payload(contexts),
            'cited_sources': cited_source_payload(answer, contexts), 'confidence': retrieval_confidence(contexts),
            'abstained': answer_is_abstention(answer),
        }
        lease.check()
        return repository.complete_turn(session_id, user_id, request_id, token, result)
