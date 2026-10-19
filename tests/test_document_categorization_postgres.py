"""Opt-in disposable database checks; never use the application database."""

import json
import os
import subprocess
from uuid import uuid4

import pytest

from tests.test_atomic_indexing_postgres import ROOT, database  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RAG_POSTGRES_TESTS") != "1", reason="opt-in disposable PostgreSQL tests")


@pytest.fixture(scope="module")
def categorized_database(database):  # noqa: F811
    _, sql = database
    for _ in range(2):
        sql((ROOT / "migrations/016_document_categorization.sql").read_text())
    return sql


def literal(value):
    return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"


def save(sql, source, patch, namespace="null"):
    statement = f"set role service_role; select save_document_categorization('{source}',{literal(patch)},'manager',{namespace});"
    return json.loads(sql(statement))


def test_pending_upload_metadata_and_partial_edits(categorized_database):
    sql = categorized_database
    category = save(sql, "upload.txt", {"document_type": "rules", "state": "Kerala"})
    assert category["values"] == {"document_type": "rules", "state": "Kerala"}
    assert category["field_sources"] == {"document_type": "manual", "state": "manual"}
    assert category["review_required"] is False
    assert sql("select metadata->>'ingest_status' from documents where source='upload.txt';").strip() == "uploaded"
    assert sql("select count(*) from legal_document_profiles;").strip() == "0"
    category = save(sql, "upload.txt", {"issued_date": "2023-01-01"})
    assert category["values"]["document_type"] == "rules"
    category = save(sql, "upload.txt", {"issued_date": None})
    assert category["overrides"]["issued_date"] is None
    assert "issued_date" in category["missing_fields"]


def test_registry_drafts_and_reviewed_profiles_are_protected(categorized_database):
    sql = categorized_database
    save(sql, "review.txt", {"document_type": "consolidated_guidelines", "issued_date": "2023-01-01"}, "'dev'")
    query = ("select profile from legal_document_profiles where namespace='dev' "
             "and document_id=(select id from documents where source='review.txt');")
    profile = json.loads(sql(query))
    assert profile["instrument_type"] == "handbook"
    assert profile["document_category"] == "consolidated_guidelines"
    assert profile["reviewed"] is False
    sql("update legal_document_profiles set profile=profile || '{\"reviewed\":true}'::jsonb where namespace='dev';")
    save(sql, "review.txt", {"document_type": "circular"}, "'dev'")
    profile = json.loads(sql(query))
    assert profile["instrument_type"] == "handbook"
    assert profile["reviewed"] is True
    # With the server write gate off, a prior namespace must not keep writing.
    category = save(sql, "review.txt", {"document_type": "rules"})
    assert "registry_namespace" not in category
    assert json.loads(sql(query))["instrument_type"] == "handbook"


def test_publication_and_metadata_edits_leave_embeddings_intact(categorized_database):
    sql = categorized_database
    category = save(sql, "published.txt", {"document_type": "rules", "issued_date": "2023-01-01"}, "'dev'")
    category["values"]["effective_date"] = "2023-07-01"
    category["field_sources"]["effective_date"] = "inferred"
    category["evidence"] = {"effective_date": [{"page": 2, "text": "Effective from 1 July 2023"}]}
    document = {"source": "published.txt", "kind": "txt", "metadata": {"categorization": category}}
    revision = json.loads(sql(f"select begin_document_revision({literal(document)});"))
    sql(f"""insert into document_revision_chunks(revision_id,document_id,source,chunk_index,content,token_estimate,metadata,embedding)
      values('{revision['id']}','{revision['document_id']}','published.txt',0,'Rules evidence',10,
             '{{"issued_date":"2023-01-01"}}',array_fill(0.01::real,array[768])::extensions.vector);
      select publish_document_revision('{revision['id']}',1);""")
    before = sql("select embedding::text from document_chunks where source='published.txt';")
    assert sql("select metadata->>'ingest_status' from documents where source='published.txt';").strip() == "indexed"
    profile = json.loads(sql(f"select profile from legal_document_profiles where document_id='{revision['document_id']}';"))
    assert profile["effective_date"] == "2023-07-01"
    assert profile["reviewed"] is False
    save(sql, "published.txt", {"issued_date": None, "effective_date": "2023-08-01"})
    metadata = json.loads(sql("select metadata from document_chunks where source='published.txt';"))
    assert metadata["issued_date"] is None
    assert metadata["effective_date"] == "2023-08-01"
    assert sql("select embedding::text from document_chunks where source='published.txt';") == before
    historical = json.loads(sql(f"select document_snapshot from document_index_revisions where id='{revision['id']}';"))
    assert historical["metadata"]["categorization"]["values"]["issued_date"] == "2023-01-01"


def test_corrections_cannot_be_lost_between_extraction_and_revision_start(categorized_database):
    sql = categorized_database
    old = save(sql, "race-category.txt", {"document_type": "rules"})
    save(sql, "race-category.txt", {"document_type": "act"})
    token = str(uuid4())
    assert sql(f"select claim_operation('documents:ingest','{token}');").strip() == "t"
    try:
        document = {"source": "race-category.txt", "kind": "txt", "metadata": {"categorization": old}}
        with pytest.raises(subprocess.CalledProcessError) as error:
            sql(f"set role service_role; select begin_categorized_revision({literal(document)},'{token}',{literal(old)});")
        assert "categorization changed" in error.value.stderr
        current = json.loads(sql("select metadata->'categorization' from documents where source='race-category.txt';"))
        document["metadata"]["categorization"] = current
        statement = f"set role service_role; select begin_categorized_revision({literal(document)},'{token}',{literal(current)});"
        result = json.loads(sql(statement))
        assert result["document_id"]
    finally:
        sql(f"select release_operation('documents:ingest','{token}');")


def test_unauthenticated_database_roles_cannot_edit_categories(categorized_database):
    for role in ("anon", "authenticated"):
        with pytest.raises(subprocess.CalledProcessError):
            categorized_database(f"set role {role}; select save_document_categorization('bad.txt','{{}}','user');")
