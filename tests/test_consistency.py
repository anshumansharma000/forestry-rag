from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import chat_service
import ingest_service
import prompts
from app import create_app
from auth import CurrentUser, get_current_user
from consistency import OperationBusy
from errors import AppError
from routers import qa


def lease():
    return nullcontext(SimpleNamespace(token='lease-token', check=lambda: None))


@pytest.mark.parametrize('answer', ['Claim [1]. Other claim [9].', 'Claim [0].', 'Claim [1, 8].',
                                   'Claim [1-3].', 'Claim [1; 2].', 'Claim [1]. Other [99', 'Claim 9].'])
def test_invalid_citations_reject_entire_answer(answer):
    assert prompts.validate_answer_citations(answer, 3, require_citation=True) == prompts.UNSUPPORTED_ANSWER


def test_citation_normalization_only_repairs_unambiguous_labels():
    assert prompts.validate_answer_citations('Claim [ 1, 1, 2 ].', 2, require_citation=True) == 'Claim [1, 2].'
    assert prompts.validate_answer_citations('Uncited claim.', 2, require_citation=True) == prompts.UNSUPPORTED_ANSWER
    assert prompts.validate_answer_citations(prompts.INSUFFICIENT_EVIDENCE_ANSWER, 0) == prompts.INSUFFICIENT_EVIDENCE_ANSWER


@pytest.mark.parametrize('answer,outcome,abstained', [
    ('Supported [1].', 'answered', False),
    (prompts.INSUFFICIENT_EVIDENCE_ANSWER, 'insufficient_evidence', True),
    (prompts.UNSUPPORTED_ANSWER, 'unsupported_answer', True),
])
def test_api_exposes_explicit_outcome(monkeypatch, answer, outcome, abstained):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser('user', 'user@example.org', 'viewer')
    monkeypatch.setattr(qa, 'retrieve', lambda *args: [])
    monkeypatch.setattr(qa, 'answer_with_gemini', lambda *args: answer)
    response = TestClient(app).post('/ask', json={'question': 'Question'})
    assert response.status_code == 200
    assert response.json()['outcome'] == outcome
    assert response.json()['abstained'] == abstained


def test_ingestion_fingerprint_covers_content_and_recipe(monkeypatch):
    doc = {'source': 'rules.txt', 'pages': [{'text': 'Fee 100'}]}
    first = ingest_service.content_fingerprint(doc)
    assert ingest_service.content_fingerprint(doc) == first
    assert ingest_service.content_fingerprint({**doc, 'pages': [{'text': 'Fee 200'}]}) != first
    monkeypatch.setenv('CHUNK_TOKENS', '1234')
    assert ingest_service.content_fingerprint(doc) != first


def test_same_document_skips_embedding_and_publication(monkeypatch):
    repo = SimpleNamespace(index_lease=lease, begin_revision=lambda doc: {'already_indexed': True})
    monkeypatch.setattr(ingest_service, 'iter_documents', lambda **kw: iter([{'source': 'rules.txt'}]))
    monkeypatch.setattr(ingest_service, 'iter_document_chunks', lambda *a: pytest.fail('Must not regenerate chunks'))
    result = ingest_service.build_index(repo, source='rules.txt')
    assert result['documents_skipped'] == 1
    assert result['documents_added'] == 0
    assert result['chunks_added'] == 0


def test_duplicate_completed_job_does_no_work(monkeypatch):
    repo = SimpleNamespace(lease=lambda _: lease(), get=lambda _: {'status': 'succeeded'})
    monkeypatch.setattr(ingest_service, 'build_index', lambda **kw: pytest.fail('Completed jobs must not run'))
    ingest_service.run_ingest_job('job', repo, document_repository=object())


@pytest.mark.parametrize('retryable,expected', [(True, 'queued'), (False, 'failed')])
def test_execution_failure_transitions_match_retry_policy(monkeypatch, retryable, expected):
    updates = []
    repo = SimpleNamespace(lease=lambda _: lease(), get=lambda _: {'status': 'queued', 'metadata': {}},
                           update=lambda *a, **kw: updates.append(kw))
    def fail(**kwargs):
        raise RuntimeError('private backend diagnostic')
    monkeypatch.setattr(ingest_service, 'build_index', fail)
    with pytest.raises(RuntimeError):
        ingest_service.run_ingest_job('job', repo, document_repository=object(), raise_on_failure=True, retryable=retryable)
    assert [update['status'] for update in updates] == ['running', expected]
    assert all(update['token'] == 'lease-token' for update in updates)
    assert 'private backend' not in updates[-1]['error']


def test_capacity_wait_stays_recoverable_without_spending_execution_attempt(monkeypatch):
    updates = []
    repo = SimpleNamespace(lease=lambda _: lease(), get=lambda _: {'status': 'queued', 'metadata': {}},
                           update=lambda *a, **kw: updates.append(kw))
    def busy(**kwargs):
        raise OperationBusy()
    monkeypatch.setattr(ingest_service, 'build_index', busy)
    with pytest.raises(OperationBusy):
        ingest_service.run_ingest_job('job', repo, document_repository=object(), raise_on_failure=True)
    assert updates[-1]['status'] == 'queued'
    assert updates[-1]['metadata']['capacity_wait'] is True


def test_chat_completed_request_replays_without_model_or_history_reads(monkeypatch):
    saved = {'answer': 'Original [1].', 'request_id': str(uuid4()), 'outcome': 'answered'}
    repo = SimpleNamespace(begin_turn=lambda *a: {'state': 'completed', 'response': saved})
    monkeypatch.setattr(chat_service, 'retrieve', lambda *a: pytest.fail('Replay must not retrieve or regenerate'))
    result = chat_service.chat_ask('session', 'question', 'user', repository=repo, request_id=saved['request_id'])
    assert result == saved


@pytest.mark.parametrize('state,status', [('busy', 409), ('conflict', 409), ('not_found', 404), ('gone', 410)])
def test_chat_conflicts_have_explicit_errors(state, status):
    repo = SimpleNamespace(begin_turn=lambda *a: {'state': state})
    with pytest.raises(AppError) as error:
        chat_service.chat_ask('session', 'question', 'user', repository=repo)
    assert error.value.status_code == status


def test_failed_generation_never_persists_a_partial_chat_turn(monkeypatch):
    repo = SimpleNamespace(begin_turn=lambda *a: {'state': 'claimed'}, turn_lease=lambda *a: lease(),
                           complete_turn=lambda *a: pytest.fail('Failure must not commit either message'))
    monkeypatch.setattr(chat_service, 'get_chat_messages', lambda *a, **kw: [])
    monkeypatch.setattr(chat_service, 'rewrite_question_for_retrieval', lambda *a: 'question')
    monkeypatch.setattr(chat_service, 'retrieve', lambda *a: [])
    def fail(*args):
        raise RuntimeError('Model unavailable')
    monkeypatch.setattr(chat_service, 'answer_with_gemini', fail)
    with pytest.raises(RuntimeError):
        chat_service.chat_ask('session', 'question', 'user', repository=repo, request_id=str(uuid4()))


def test_recovery_dispatches_both_job_types_and_leaves_outages_retryable(monkeypatch):
    import job_recovery
    updates, dispatched = [], []
    rows = [{'id': 'ingest', 'kind': 'documents.ingest'}, {'id': 'lab', 'kind': 'rag_lab.build'}]
    repo = SimpleNamespace(recover=lambda: rows, update=lambda *a, **kw: updates.append(kw))
    monkeypatch.setattr(job_recovery, 'celery_broker_url', lambda: 'configured')
    monkeypatch.setattr(job_recovery, 'IngestJobRepository', lambda: repo)
    monkeypatch.setattr(job_recovery, 'enqueue_ingest_job', lambda job: dispatched.append(job) or 'task-id')
    def fail(job):
        dispatched.append(job)
        raise ConnectionError('Broker unavailable')
    monkeypatch.setattr(job_recovery, 'enqueue_rag_lab_job', fail)
    job_recovery.recover_jobs()
    assert dispatched == ['ingest', 'lab']
    assert len(updates) == 1
    assert updates[0]['status'] == 'queued'


def test_delayed_job_does_not_execute_or_consume_attempt(monkeypatch):
    from datetime import UTC, datetime, timedelta
    job = {'status': 'queued', 'available_at': (datetime.now(UTC) + timedelta(minutes=2)).isoformat()}
    repo = SimpleNamespace(lease=lambda _: lease(), get=lambda _: job,
                           update=lambda *a, **kw: pytest.fail('Backoff must not consume an attempt'))
    with pytest.raises(OperationBusy) as error:
        ingest_service.run_ingest_job('job', repo, document_repository=object())
    assert int(error.value.headers['Retry-After']) >= 119
