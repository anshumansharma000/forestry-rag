from fastapi import APIRouter, Depends, HTTPException, status

from auth import CurrentUser, require_roles
from prompts import answer_outcome
from rag import answer_is_abstention, answer_with_gemini, cited_source_payload, retrieval_confidence, retrieve, source_payload
from schemas import AskRequest, AskResponse
from token_usage import track_query_usage

router = APIRouter(tags=["qa"])


@router.post("/ask", response_model=AskResponse)
@track_query_usage
def ask(request_body: AskRequest, _user: CurrentUser = Depends(require_roles("viewer"))):
    if not request_body.question.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="question is required")

    contexts = retrieve(request_body.question, request_body.top_k)
    answer = answer_with_gemini(request_body.question, contexts)
    return {
        "answer": answer,
        "outcome": answer_outcome(answer),
        "sources": source_payload(contexts),
        "cited_sources": cited_source_payload(answer, contexts),
        "confidence": retrieval_confidence(contexts),
        "abstained": answer_is_abstention(answer),
    }
