"""Environment-scoped legal annotations; never modifies the shared corpus."""
import os
import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from rag_errors import RagError
from services.storage import supabase_client

InstrumentType = Literal['unknown', 'handbook', 'act', 'rules', 'amendment', 'guideline',
                         'notification', 'clarification', 'instruction', 'judicial']
STAGES = {
    'handbook': ['handbook'],
    'act': ['act'],
    'rules': ['rules'],
    'amendment': ['amendment'],
    'implementation': ['guideline', 'notification', 'clarification', 'instruction'],
    'judicial': ['judicial'],
}


class LegalRelationship(BaseModel):
    target_document_id: UUID
    relation: Literal['amends', 'supersedes', 'repeals', 'interprets', 'stays', 'clarifies', 'refers_to']
    source_provision: str = Field(min_length=1, max_length=500)
    target_provision: str = Field(min_length=1, max_length=500)
    effective_date: date | None = None
    evidence: str = Field(min_length=1, max_length=2000)


class LegalProfile(BaseModel):
    instrument_type: InstrumentType = 'unknown'
    issued_date: date | None = None
    effective_date: date | None = None
    jurisdiction: str | None = Field(default=None, max_length=500)
    enabling_provision: str | None = Field(default=None, max_length=500)
    judicial_status: Literal['unknown', 'interim', 'final', 'modified', 'stayed', 'set_aside'] = 'unknown'
    identifiers: list[str] = Field(default_factory=list, max_length=50)
    relationships: list[LegalRelationship] = Field(default_factory=list, max_length=50)
    reviewed: bool = False
    notes: str | None = Field(default=None, max_length=4000)


def legal_namespace() -> str:
    value = os.getenv('LEGAL_REGISTRY_NAMESPACE', '').strip()
    if not value or len(value) > 64 or not all(c.isalnum() or c in '_-' for c in value):
        raise RagError('Set LEGAL_REGISTRY_NAMESPACE to an explicit environment name before using legal retrieval.')
    return value


class LegalRegistryRepository:
    def __init__(self, client=None):
        self.namespace = legal_namespace()
        self.client = client or supabase_client()

    def profiles(self) -> dict[str, dict]:
        rows = []
        for offset in range(0, 10001, 500):
            batch = self.client.table('legal_document_profiles').select('document_id,profile').eq(
                'namespace', self.namespace
            ).order('document_id').range(offset, offset + 499).execute().data or []
            rows.extend(batch)
            if len(rows) > 10000:
                raise RagError('Legal registry exceeds the 10000-document retrieval limit.')
            if len(batch) < 500:
                return {str(row['document_id']): row['profile'] for row in rows}
        raise RagError('Legal registry pagination incomplete.')

    def save(self, document_id: str, profile: LegalProfile, reviewer_id: str) -> dict:
        row = {'namespace': self.namespace, 'document_id': document_id,
               'profile': profile.model_dump(mode='json'), 'reviewer_id': reviewer_id,
               'updated_at': datetime.now(UTC).isoformat()}
        return self.client.table('legal_document_profiles').upsert(
            row, on_conflict='namespace,document_id'
        ).execute().data[0]

    def match(self, embedding: list[float], query: str, kinds: list[str], count: int,
              document_ids: list[str] | None = None) -> list[dict]:
        return self.client.rpc('match_legal_chunks_v1', {
            'registry_namespace': self.namespace, 'query_embedding': embedding,
            'query_text': query, 'instrument_types': kinds, 'match_count': count,
            'document_ids': document_ids,
        }).execute().data or []


def suggest_profile(document: dict) -> LegalProfile:
    """Title-based draft only; no inferred dates, legal status, or supersession links."""
    from temporal import parse_date

    title = f"{document.get('title') or ''} {document.get('source') or ''}"
    patterns = (
        ('handbook', r'handbook|consolidated guidelines'),
        ('judicial', r'judgment|judgement|supreme court|high court|judicial'),
        ('amendment', r'amendment|amending'),
        ('clarification', r'clarification'),
        ('guideline', r'guidelines?'),
        ('rules', r'rules?'),
        ('act', r'adhiniyam|act'),
        ('notification', r'notification'),
        ('instruction', r'instruction'),
    )
    kind = next((kind for kind, pattern in patterns if re.search(r'\b(?:' + pattern + r')\b', title, re.I)), 'unknown')
    metadata = document.get('metadata') or {}
    return LegalProfile(instrument_type=kind, issued_date=parse_date(metadata.get('issued_date')),
                        effective_date=parse_date(metadata.get('effective_date')),
                        notes='Unreviewed suggestion from title and existing date metadata. Verify against the original document.')
