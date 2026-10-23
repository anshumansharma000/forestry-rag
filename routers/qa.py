from fastapi import APIRouter, Depends, HTTPException, status

from auth import CurrentUser, require_roles
from prompts import answer_outcome
from query_cache import answer_cached, current_access, request_access
from query_context import request_context
from query_scope import scope_response
from rag import answer_is_abstention, answer_with_gemini, cited_source_payload, retrieval_confidence, retrieve, source_payload
from schemas import AskRequest, AskResponse
from token_usage import track_query_usage
from web_fallback import apply_web_fallback

router = APIRouter(tags=["qa"])


@router.post("/ask", response_model=AskResponse)
@track_query_usage
@request_access("_user")
@request_context("request_body")
def ask(request_body: AskRequest, _user: CurrentUser = Depends(require_roles("viewer"))):
    if not request_body.question.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="question is required")
    blocked = scope_response(request_body.question)
    if blocked is not None:
        return blocked

    return answer_cached(request_body.question, request_body.top_k, lambda: _generate(request_body))


def _generate(request_body):
    contexts = retrieve(request_body.question, request_body.top_k)
    answer = answer_with_gemini(request_body.question, contexts)
    return apply_web_fallback({
        "answer": answer,
        "outcome": answer_outcome(answer),
        "sources": source_payload(contexts),
        "cited_sources": cited_source_payload(answer, contexts),
        "confidence": retrieval_confidence(contexts),
        "abstained": answer_is_abstention(answer),
    }, request_body.question, user_id=current_access(), scope_checked=True)
