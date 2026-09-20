"""New admin-only endpoints; existing request and response contracts stay unchanged."""
import os
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import CurrentUser, require_exact_admin
from legal_registry import LegalProfile, LegalRegistryRepository, suggest_profile
from legal_retrieval import retrieve_legal
from prompts import answer_is_abstention, answer_outcome, answer_with_gemini
from repositories import DocumentRepository
from retrieval import cited_source_payload, retrieval_confidence, source_payload
from schemas import AskRequest, AskResponse
from token_usage import track_query_usage

router = APIRouter(prefix='/admin/legal', tags=['legal'])


class LegalPreviewRequest(AskRequest):
    as_of: date | None = None
    jurisdiction: str | None = Field(default=None, max_length=500)
    generate_answer: bool = False


class LegalPreviewResponse(AskResponse):
    review: dict = Field(default_factory=dict)
    evidence: list[dict] = Field(default_factory=list)


class LegalProfilesResponse(BaseModel):
    namespace: str
    profiles: dict[str, LegalProfile]


@router.get('/profiles', response_model=LegalProfilesResponse)
def profiles(_user: CurrentUser = Depends(require_exact_admin)):
    repository = LegalRegistryRepository()
    return {'namespace': repository.namespace, 'profiles': repository.profiles()}


@router.put('/profiles/{document_id}', response_model=LegalProfile)
def save_profile(document_id: UUID, body: LegalProfile, user: CurrentUser = Depends(require_exact_admin)):
    # Independent write gate: previewing the shared corpus never implies permission to annotate production.
    if os.getenv('LEGAL_REGISTRY_WRITES_ENABLED', 'false').lower() != 'true':
        raise HTTPException(403, 'Legal registry writes are disabled for this deployment.')
    repository = LegalRegistryRepository()
    documents = DocumentRepository()
    for target in {str(document_id), *(str(link.target_document_id) for link in body.relationships)}:
        if not documents.get_document(target):
            raise HTTPException(404, f'Document not found: {target}')
    if any(link.target_document_id == document_id for link in body.relationships):
        raise HTTPException(422, 'A legal relationship must reference a different document.')
    repository.save(str(document_id), body, user.id)
    return body


@router.post('/preview', response_model=LegalPreviewResponse)
@track_query_usage
def preview(body: LegalPreviewRequest, _user: CurrentUser = Depends(require_exact_admin)):
    contexts = retrieve_legal(body.question, body.top_k, options={
        'as_of': body.as_of.isoformat() if body.as_of else None,
        'jurisdiction': body.jurisdiction,
    })
    answer = answer_with_gemini(body.question, contexts) if body.generate_answer else ''
    return {
        'answer': answer,
        'outcome': answer_outcome(answer),
        'sources': source_payload(contexts),
        'cited_sources': cited_source_payload(answer, contexts),
        'confidence': retrieval_confidence(contexts),
        'abstained': answer_is_abstention(answer) if body.generate_answer else False,
        'review': contexts[0]['metadata']['legal_review'] if contexts else {
            'missing_reviewed_evidence': 'all', 'collection_currentness': 'unverified', 'no_evidence': True,
        },
        'evidence': [{'document_id': ctx['document_id'], 'chunk_index': ctx['chunk_index'],
                      'profile': ctx['metadata']['legal_profile'],
                      'search_stages': ctx['metadata']['legal_search_stages']} for ctx in contexts],
    }


@router.get('/profiles/{document_id}/suggestion', response_model=LegalProfile)
def profile_suggestion(document_id: UUID, _user: CurrentUser = Depends(require_exact_admin)):
    document = DocumentRepository().get_document(str(document_id))
    if not document:
        raise HTTPException(404, 'Document not found.')
    return suggest_profile(document)
