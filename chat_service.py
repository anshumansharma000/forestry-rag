from prompts import answer_with_gemini, rewrite_question_for_retrieval
from rag_errors import RagError
from repositories import ChatRepository
from retrieval import retrieve, source_payload


def create_chat_session(title: str | None = None, user_id: str | None = None, repository: ChatRepository | None = None) -> dict:
    repository = repository or ChatRepository()
    return repository.create_session(title, user_id)


def list_chat_sessions(user_id: str, limit: int = 20, repository: ChatRepository | None = None) -> list[dict]:
    repository = repository or ChatRepository()
    return repository.list_sessions(user_id, limit)


def get_chat_messages(session_id: str, user_id: str, limit: int | None = None, repository: ChatRepository | None = None) -> list[dict]:
    repository = repository or ChatRepository()
    return repository.get_messages(session_id, user_id, limit)


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


def chat_ask(session_id: str, message: str, user_id: str, top_k: int | None = None, repository: ChatRepository | None = None) -> dict:
    if not message.strip():
        raise RagError("message is required")

    repository = repository or ChatRepository()
    previous_messages = get_chat_messages(session_id, user_id, repository=repository)
    user_message = save_chat_message(session_id, "user", message, repository=repository)
    history_with_latest = previous_messages + [user_message]
    search_query = rewrite_question_for_retrieval(previous_messages, message)
    contexts = retrieve(search_query, top_k)
    answer = answer_with_gemini(message, contexts, history_with_latest)
    sources = source_payload(contexts)
    assistant_message = save_chat_message(
        session_id,
        "assistant",
        answer,
        sources=sources,
        metadata={"search_query": search_query},
        repository=repository,
    )
    return {
        "session_id": session_id,
        "user_message": user_message,
        "assistant_message": assistant_message,
        "search_query": search_query,
        "answer": answer,
        "sources": sources,
    }
