import logging
from copy import deepcopy

import pytest

import jev_shadow
import prompts
from jev_settings import ADDITIONAL_FEATURES, invalid_settings, mode
from tests.test_lite_routing import source


@pytest.mark.parametrize("feature", ADDITIONAL_FEATURES)
def test_new_features_accept_active(monkeypatch, feature):
    monkeypatch.setenv(f"JEV_{feature.upper()}_MODE", "active")
    assert mode(feature) == "active"
    assert f"JEV_{feature.upper()}_MODE" not in invalid_settings()


def test_model_and_planning_proposals_do_not_change_answer_pipeline(monkeypatch, caplog):
    monkeypatch.setenv("JEV_ROUTING_MODE", "shadow")
    monkeypatch.setenv("JEV_PLANNING_MODE", "shadow")
    calls = []
    def evaluate(state, questions, **kw):
        calls.append((state, questions))
        assert "baseline_route" not in state and "plan" not in state
        return {"extraction": 1, "synthesis": 0, "legal_risk": 0, "planning": 0}
    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    operations = []
    def structured(*args, **kw):
        operations.append(kw["operation"])
        if kw["operation"] == "plan":
            return {"central_answer": "Permit requires approval", "conflicts": []}
        return {"supported": True, "unchanged": True, "answer": "", "issues": []}
    def generate(*args, **kw):
        operations.append(kw["operation"])
        return "Approval is required [1]."
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", structured)
    monkeypatch.setattr(prompts, "generate_with_gemini", generate)
    with caplog.at_level(logging.INFO):
        answer = prompts.answer_with_gemini("Give an overview of the permit requirements.", [source()])
    assert answer == "Approval is required [1]."
    assert operations == ["plan", "answer_complex", "verify"]
    assert len(calls) == 1
    records = {r.operation: r for r in caplog.records if r.msg == "jev_policy_decision"}
    assert records["routing"].baseline_route == "answer_complex"
    assert records["routing"].proposed_route == "answer_direct"
    assert records["planning"].baseline_planning is True
    assert records["planning"].proposed_planning is False
    assert records["routing"].disagreement and records["planning"].disagreement


def test_independent_switches_and_unavailable_results(monkeypatch, caplog):
    monkeypatch.setenv("JEV_PLANNING_MODE", "shadow")
    def evaluate(state, questions, **kw):
        assert set(questions) == {"planning"}
        return None
    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    with caplog.at_level(logging.INFO):
        jev_shadow.answer_policy("question", "evidence", "", baseline_route="answer_complex", baseline_planning=True)
    record = next(r for r in caplog.records if r.msg == "jev_policy_decision")
    assert record.proposed_planning is None and record.disagreement is None


def test_disabled_features_make_no_calls(monkeypatch):
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: pytest.fail("Unexpected Jev call"))
    jev_shadow.answer_policy("question", "evidence", "", baseline_route="answer_direct", baseline_planning=False)
    jev_shadow.history_selection([], "question", [])
    # Off mode must not require a parsed document or chunking fields.
    jev_shadow.document_policy({}, baseline_profile=None, document_ref="test", origin="ingest")


def test_history_keeps_protected_messages_and_does_not_mutate(monkeypatch, caplog):
    monkeypatch.setenv("JEV_HISTORY_MODE", "shadow")
    monkeypatch.setenv("RAG_HISTORY_SELECTION_TOKENS", "1")
    messages = [{"role": "user", "content": "Keep my constraint"},
                {"role": "assistant", "content": "Unrelated reply"},
                {"role": "user", "content": "Return to the first subject"},
                {"role": "assistant", "content": "Latest reply"}]
    original = deepcopy(messages)
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: {"keep_1": 0})
    with caplog.at_level(logging.INFO):
        assert jev_shadow.history_selection(messages, "question", messages) is messages
    assert messages == original
    record = next(r for r in caplog.records if r.msg == "jev_policy_decision")
    assert record.proposed_indices == [0, 2, 3]
    assert record.baseline_indices == [0, 1, 2, 3]
    assert record.disagreement is True


def test_document_sampling_is_bounded_nonmutating_and_metered(monkeypatch, caplog):
    monkeypatch.setenv("JEV_CLASSIFICATION_MODE", "shadow")
    monkeypatch.setenv("JEV_EXTRACTION_MODE", "shadow")
    doc = {"title": "Private title", "source": "private.pdf", "metadata": {"document_type": "rules"},
           "pages": [{"page": i, "text": "confidential " * 700} for i in range(1, 11)]}
    original = deepcopy(doc)
    seen = []
    def evaluate(state, questions, **kw):
        seen.append(state)
        assert [p["page"] for p in state["pages"]] == [1, 6, 10]
        assert all(len(p["text"]) == 4000 and p["truncated"] for p in state["pages"])
        return {k: 1 if k in {"profile_faq", "kind_amendment", "quality_1"} else 0 for k in questions}
    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    with caplog.at_level(logging.INFO):
        assert jev_shadow.document_policy(doc, baseline_profile="procedure", document_ref="private.pdf",
                                         origin="rag_lab") == {"review_required": False}
    assert len(seen) == 1 and doc == original
    records = {r.operation: r for r in caplog.records if r.msg == "jev_policy_decision"}
    assert records["classification"].proposed_profile == "faq"
    assert records["classification"].baseline_profile == "procedure"
    assert records["extraction"].proposed_review_pages == [6]
    assert records["classification"].query_id
    assert any(r.msg == "query_generation_usage" for r in caplog.records)
    for record in records.values():
        assert "private.pdf" not in str(record.__dict__)
        assert "confidential" not in str(record.__dict__)


def test_uncertain_classification_does_not_force_a_label():
    assert jev_shadow.confident_label({"profile_faq": .81, "profile_section": .8},
                                      "profile", ("faq", "section")) == "uncertain"


def test_active_routes_are_applied_with_risk_guards(monkeypatch):
    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    proposal = {"route": "answer_direct"}
    kwargs = {"evidence_plan": None, "history": False}
    assert jev_shadow.apply_route(proposal, "answer_complex", "List the steps.", [source()], **kwargs) == "answer_direct"
    assert jev_shadow.apply_route(proposal, "answer_complex_high", "List the steps.", [source()], **kwargs) == "answer_complex_high"
    assert jev_shadow.apply_route(proposal, "answer_complex", "List the steps.",
                                 [source("Bamboo is exempt but needs approval.")], **kwargs) == "answer_complex"
    assert jev_shadow.apply_route(proposal, "answer_complex", "List the steps.", [source()],
                                 evidence_plan={"conflicts": ["conflict"]}, history=False) == "answer_complex"
    assert jev_shadow.apply_route(proposal, "answer_complex", "List the steps.", [source()],
                                 evidence_plan=None, history=True) == "answer_complex"
    assert jev_shadow.apply_route({"route": "answer_complex_high"}, "answer_direct", "Question", [source()],
                                 **kwargs) == "answer_complex_high"


def test_active_planning_retains_risk_and_explicit_disable(monkeypatch):
    monkeypatch.setenv("JEV_PLANNING_MODE", "active")
    kwargs = {"baseline_route": "answer_complex", "history": False, "allowed": True}
    assert not jev_shadow.apply_planning({"planning": False}, True, "List steps.", [source()], **kwargs)
    assert jev_shadow.apply_planning({"planning": False}, True, "List steps.", [source()],
                                    **{**kwargs, "baseline_route": "answer_complex_high"})
    assert not jev_shadow.apply_planning({"planning": True}, False, "Question", [source()],
                                        **{**kwargs, "allowed": False})
    assert jev_shadow.apply_planning({"planning": True}, False, "Question", [source()], **kwargs)


def test_active_route_change_forces_verification(monkeypatch):
    monkeypatch.setenv("JEV_ROUTING_MODE", "active")
    monkeypatch.setenv("RAG_ANSWER_VERIFICATION", "false")
    monkeypatch.setenv("RAG_RISK_BASED_VERIFICATION", "false")
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: {"extraction": 0, "synthesis": .8, "legal_risk": 1})
    operations = []
    def generate(*a, **kw):
        operations.append(kw["operation"])
        return "Approval is required [1]."
    def verify(*a, **kw):
        operations.append(kw["operation"])
        return {"supported": True, "unchanged": True, "issues": [], "answer": ""}
    monkeypatch.setattr(prompts, "generate_with_gemini", generate)
    monkeypatch.setattr(prompts, "generate_structured_with_gemini", verify)
    prompts.answer_with_gemini("Who approves the permit?", [source()])
    assert operations == ["answer_complex_high", "verify_complex"]


def test_active_history_only_removes_unrelated_old_assistant(monkeypatch):
    monkeypatch.setenv("JEV_HISTORY_MODE", "active")
    monkeypatch.setenv("RAG_HISTORY_SELECTION_TOKENS", "1")
    messages = [{"role": "user", "content": "constraint"}, {"role": "assistant", "content": "unrelated"},
                {"role": "assistant", "content": "latest"}]
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: {"keep_1": 0})
    assert jev_shadow.history_selection(messages, "question", messages) == [messages[0], messages[2]]
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: None)
    assert jev_shadow.history_selection(messages, "question", messages) is messages


def test_active_document_policy_honors_explicit_profile_and_blocks_only_strong_corruption(monkeypatch):
    monkeypatch.setenv("JEV_CLASSIFICATION_MODE", "active")
    monkeypatch.setenv("JEV_EXTRACTION_MODE", "active")
    doc = {"source": "file.txt", "title": "file", "pages": [{"page": 1, "text": "text"}], "metadata": {}}
    def evaluate(state, questions, **kw):
        return {key: 1 if key in {"profile_faq", "kind_rules", "quality_0"} else 0 for key in questions}
    monkeypatch.setattr(jev_shadow, "evaluate", evaluate)
    result = jev_shadow.document_policy(doc, baseline_profile=None, document_ref="test", origin="ingest")
    assert result == {"profile": "faq", "instrument_type_suggestion": "rules", "review_required": True,
                      "extraction_review_pages": [1]}
    result = jev_shadow.document_policy(doc, baseline_profile="procedure", document_ref="test", origin="rag_lab")
    assert result == {"instrument_type_suggestion": "rules", "review_required": True,
                      "extraction_review_pages": [1]}
    assert doc["metadata"] == {}


def test_active_quality_failure_never_publishes_index(monkeypatch):
    from types import SimpleNamespace

    import ingest_service
    from errors import AppError
    from tests.test_consistency import lease

    monkeypatch.setenv("JEV_EXTRACTION_MODE", "active")
    doc = {"source": "file.txt", "title": "file", "pages": [{"page": 1, "text": "garbled"}], "metadata": {}}
    failures = []
    repo = SimpleNamespace(index_lease=lease, begin_revision=lambda doc: {"id": "revision"},
                           fail_revision=lambda *a, **kw: failures.append(a), record_ingest_failure=lambda *a, **kw: None,
                           publish_revision=lambda *a, **kw: pytest.fail("Must not publish corrupted extraction"))
    monkeypatch.setattr(ingest_service, "iter_documents", lambda **kw: iter([doc]))
    monkeypatch.setattr(jev_shadow, "evaluate", lambda *a, **kw: {"quality_0": 1})
    with pytest.raises(AppError, match="needs review"):
        ingest_service.build_index(repo, source="file.txt")
    assert len(failures) == 1


def test_rag_lab_keeps_jev_quality_findings_as_queryable_warnings(monkeypatch):
    from types import SimpleNamespace

    import rag_lab_service

    monkeypatch.setenv("JEV_EXTRACTION_MODE", "active")
    monkeypatch.setenv("JEV_RAG_LAB_EXTRACTION_POLICY", "warn")
    doc = {"source": "rules.txt", "kind": "txt", "title": "Rules", "metadata": {},
           "pages": [{"page": 1, "text": "Rule 1. Timber transport requires approval."}]}
    rows = []
    writes = []
    repo = SimpleNamespace(
        get_revision=lambda _: {"status": "queued", "experiment_id": "experiment",
                                "config": {"chunking": {"profile": "auto", "max_tokens": 200,
                                                         "overlap_tokens": 0}}},
        list_files=lambda _: [{"id": "file", "filename": "rules.txt"}],
        delete_revision_chunks=lambda _: None,
        update_revision=lambda *a, **kw: writes.append(kw), update_experiment=lambda *a, **kw: None,
        insert_chunk_batch=lambda batch: rows.extend(batch) or len(batch),
    )
    monkeypatch.setattr(rag_lab_service, "extracted_document", lambda *a: doc)
    monkeypatch.setattr(jev_shadow, "evaluate", lambda state, questions, **kw:
                        {key: 1 for key in questions})
    monkeypatch.setattr(rag_lab_service, "embed_texts", lambda texts: [[1.0] for _ in texts])

    result = rag_lab_service.build_revision("revision", repository=repo, storage=SimpleNamespace())

    assert result["chunks"] == 1
    assert result["warnings"] == [{"file_id": "file", "filename": "rules.txt",
                                   "code": "jev_extraction_review", "pages": [1]}]
    assert rows[0]["metadata"]["jev_extraction_review_required"] is True
    assert writes[-1] == {"status": "ready", "chunk_count": 1}


def test_rag_lab_falls_back_when_jev_profile_produces_no_chunks(monkeypatch):
    from types import SimpleNamespace

    import rag_lab_service

    monkeypatch.setenv("JEV_CLASSIFICATION_MODE", "active")
    doc = {"source": "rules.txt", "kind": "txt", "title": "Rules", "metadata": {},
           "pages": [{"page": 1, "text": "Rule 1. Timber transport requires approval."}]}
    rows = []
    repo = SimpleNamespace(
        get_revision=lambda _: {"status": "queued", "experiment_id": "experiment",
                                "config": {"chunking": {"profile": "auto", "max_tokens": 200,
                                                         "overlap_tokens": 0}}},
        list_files=lambda _: [{"id": "file", "filename": "rules.txt"}],
        delete_revision_chunks=lambda _: None,
        update_revision=lambda *a, **kw: None, update_experiment=lambda *a, **kw: None,
        insert_chunk_batch=lambda batch: rows.extend(batch) or len(batch),
    )
    monkeypatch.setattr(rag_lab_service, "extracted_document", lambda *a: doc)
    monkeypatch.setattr(jev_shadow, "evaluate", lambda state, questions, **kw:
                        {key: 1 if key == "profile_faq" else 0 for key in questions})
    monkeypatch.setattr(rag_lab_service, "embed_texts", lambda texts: [[1.0] for _ in texts])

    result = rag_lab_service.build_revision("revision", repository=repo, storage=SimpleNamespace())

    assert result["chunks"] == 1
    assert result["warnings"][0]["code"] == "jev_chunk_profile_fallback"
    assert rows[0]["metadata"]["profile"] == "section"
    assert rows[0]["metadata"]["jev_chunk_profile_suggestion"] == "faq"
    assert rows[0]["metadata"]["jev_chunk_profile_fallback"] == "auto"


def test_active_classification_changes_index_profile_and_recipe(monkeypatch):
    from types import SimpleNamespace

    import ingest_service
    from tests.test_consistency import lease

    doc = {"source": "file.txt", "title": "file", "pages": [{"page": 1, "text": "text"}], "metadata": {}}
    original_fingerprint = ingest_service.content_fingerprint(doc)
    monkeypatch.setenv("JEV_CLASSIFICATION_MODE", "active")
    assert ingest_service.content_fingerprint(doc) != original_fingerprint
    used = []
    repo = SimpleNamespace(index_lease=lease, begin_revision=lambda doc: {"id": "revision"},
                           publish_revision=lambda *a, **kw: None)
    monkeypatch.setattr(ingest_service, "iter_documents", lambda **kw: iter([doc]))
    monkeypatch.setattr(jev_shadow, "evaluate", lambda state, questions, **kw:
                        {key: 1 if key in {"profile_faq", "kind_rules"} else 0 for key in questions})
    def persist(*a, **kw):
        used.append(kw["profile"])
        assert a[2]["metadata"]["jev_instrument_type_suggestion"] == "rules"
        return 1
    monkeypatch.setattr(ingest_service, "persist_document_chunks", persist)
    assert ingest_service.build_index(repo)["documents_added"] == 1
    assert used == ["faq"]


@pytest.mark.parametrize("existing,frozen,expected", [(False, None, "faq"), (True, None, "auto"),
                                                       (True, "procedure", "procedure")])
def test_lab_profile_is_recovered_from_committed_chunks_on_resume(monkeypatch, existing, frozen, expected):
    from types import SimpleNamespace

    import rag_lab_service

    monkeypatch.setenv("JEV_CLASSIFICATION_MODE", "active")
    config = {"chunking": {"profile": "auto", "max_tokens": 200, "overlap_tokens": 0}}
    writes = []
    repo = SimpleNamespace(
        get_revision=lambda _: {"status": "failed" if existing else "queued", "experiment_id": "experiment", "config": config},
        list_files=lambda _: [{"id": "file", "filename": "file.txt"}],
        existing_chunk_keys=lambda _: {("file", 0)}, delete_revision_chunks=lambda _: None,
        existing_chunk_profiles=lambda _: {"file": frozen} if frozen else {},
        update_revision=lambda *a, **kw: writes.append(kw), update_experiment=lambda *a: None)
    doc = {"source": "file.txt", "title": "file", "pages": [{"page": 1, "text": "text"}], "metadata": {}}
    monkeypatch.setattr(rag_lab_service, "extracted_document", lambda *a: doc)
    monkeypatch.setattr(jev_shadow, "evaluate", lambda state, questions, **kw:
                        {key: 1 if key == "profile_faq" else 0 for key in questions})
    def chunks(doc, **kwargs):
        assert kwargs["profile"] == expected
        yield {"source": "file.txt", "chunk_index": 1, "chunk_type": "text", "section_heading": None,
               "page_start": 1, "page_end": 1, "content": "text", "token_estimate": 1,
               "metadata": {"unit_types": ["faq"]} if expected == "faq" else {}}
    monkeypatch.setattr(rag_lab_service, "iter_document_chunks", chunks)
    monkeypatch.setattr(rag_lab_service, "insert_embedded_batch", lambda repository, batch: len(batch))
    result = rag_lab_service.build_revision("revision", repository=repo, storage=SimpleNamespace())
    assert result["chunks"] == (2 if existing else 1)
    assert not any("config" in write for write in writes)


@pytest.mark.parametrize("fenced", [False, True])
def test_real_repository_profile_reader_paginates_and_rejects_mixed_recipes(fenced):
    from types import SimpleNamespace

    from rag_lab_repository import FencedLabRepository, RagLabRepository

    rows = [{"file_id": "one", "metadata": {"profile": "faq"}} for _ in range(500)]
    rows.append({"file_id": "two", "metadata": {"profile": "procedure"}})
    class Query:
        offset = 0
        def select(self, fields):
            assert fields == "file_id,metadata"
            return self
        def eq(self, field, value):
            assert (field, value) == ("revision_id", "revision")
            return self
        def order(self, field):
            assert field == "id"
            return self
        def range(self, start, end):
            self.offset = start
            assert end - start == 499
            return self
        def execute(self):
            return SimpleNamespace(data=rows[self.offset:self.offset + 500])
    client = SimpleNamespace(table=lambda name: Query())
    repo = FencedLabRepository(client, "job", "token") if fenced else RagLabRepository(client)
    assert repo.existing_chunk_profiles("revision") == {"one": "faq", "two": "procedure"}
    rows.append({"file_id": "one", "metadata": {"profile": "section"}})
    with pytest.raises(ValueError, match="Mixed saved"):
        repo.existing_chunk_profiles("revision")
