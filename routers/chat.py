from fastapi import APIRouter, Depends, Request

from auth import CurrentUser, audit_event, require_roles
from rag import (
    chat_ask,
    create_chat_session,
    delete_chat_message,
    delete_chat_session,
    get_chat_messages,
    list_chat_sessions,
)
from schemas import ChatAskRequest, ChatSessionResponse, CreateChatSessionRequest

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/sessions", response_model=ChatSessionResponse)
def create_session(
    request_body: CreateChatSessionRequest = CreateChatSessionRequest(),
    user: CurrentUser = Depends(require_roles("viewer")),
):
    return create_chat_session(request_body.title, user.id)


@router.get("/sessions")
def sessions(user: CurrentUser = Depends(require_roles("viewer"))):
    return {"sessions": list_chat_sessions(user.id)}


@router.get("/sessions/{session_id}/messages")
def session_messages(session_id: str, user: CurrentUser = Depends(require_roles("viewer"))):
    return {"session_id": session_id, "messages": get_chat_messages(session_id, user.id)}


@router.delete("/sessions/{session_id}")
def remove_session(
    request: Request,
    session_id: str,
    user: CurrentUser = Depends(require_roles("viewer")),
):
    result = delete_chat_session(session_id, user.id)
    audit_event(request, user, "chat.session.delete", "chat_session", session_id)
    return result


@router.delete("/sessions/{session_id}/messages/{message_id}")
def remove_message(
    request: Request,
    session_id: str,
    message_id: str,
    user: CurrentUser = Depends(require_roles("viewer")),
):
    result = delete_chat_message(session_id, message_id, user.id)
    audit_event(request, user, "chat.message.delete", "chat_message", message_id, {"session_id": session_id})
    return result


@router.post("/sessions/{session_id}/ask")
def ask_in_session(
    session_id: str,
    request_body: ChatAskRequest,
    user: CurrentUser = Depends(require_roles("viewer")),
):
    return chat_ask(session_id, request_body.message, user.id, request_body.top_k)
