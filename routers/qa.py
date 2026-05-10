from fastapi import APIRouter, Depends, HTTPException, status

from auth import CurrentUser, require_roles
from rag import answer_with_gemini, retrieve, source_payload
from schemas import AskRequest, AskResponse

router = APIRouter(tags=["qa"])


@router.post("/ask", response_model=AskResponse)
def ask(request_body: AskRequest, _user: CurrentUser = Depends(require_roles("viewer"))):
    if not request_body.question.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="question is required")

    contexts = retrieve(request_body.question, request_body.top_k)
    answer = answer_with_gemini(request_body.question, contexts)
    return {
        "answer": answer,
        "sources": source_payload(contexts),
    }
