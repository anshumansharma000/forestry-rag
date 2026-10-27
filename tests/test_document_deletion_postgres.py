"""Opt-in disposable database checks: RAG_POSTGRES_TESTS=1 pytest -q tests/test_document_deletion_postgres.py."""
import json
import os
import subprocess
from uuid import uuid4

import pytest

from tests.test_atomic_indexing_postgres import ROOT, database  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv('RAG_POSTGRES_TESTS') != '1', reason='opt-in disposable Docker PostgreSQL tests')


@pytest.fixture(scope='module')
def deletion_database(database):  # noqa: F811
    _, sql = database
    for path in ('migrations/014_answer_cache_versions.sql', 'migrations/017_admin_document_deletion.sql'):
        sql((ROOT / path).read_text())
    sql('grant insert on audit_events to service_role; grant select on ingest_jobs to service_role;')
    return sql


def test_deletion_cascades_invalidates_cache_and_records_audit(deletion_database):
    sql = deletion_database
    actor, token = str(uuid4()), str(uuid4())
    revision = json.loads(sql("select begin_document_revision('{\"source\":\"delete.txt\",\"kind\":\"txt\"}');"))
    doc = revision['document_id']
    sql(f"""insert into document_revision_chunks(revision_id,document_id,source,chunk_index,content,token_estimate,embedding)
      values('{revision['id']}','{doc}','delete.txt',0,'Delete evidence',10,array_fill(0.01::real,array[768])::extensions.vector);
      select publish_document_revision('{revision['id']}',1);
      insert into documents(source,kind) values('keep.txt','txt');""")
    before = int(sql("select generation from answer_cache_generations where scope='corpus';"))
    sql(f"select claim_operation('documents:ingest','{token}');")
    try:
        prepared = json.loads(sql(f"set role service_role; select prepare_document_delete('{doc}','{token}');"))
        assert prepared == {'status': 'ready', 'source': 'delete.txt'}
        audit = json.dumps({'actor_user_id': actor, 'actor_email': 'admin@example.com', 'actor_role': 'admin'})
        sql(f"set role service_role; select finish_document_delete('{doc}','{token}','{audit}');")
        # Safe retry after the first request committed but its response was lost.
        sql(f"set role service_role; select finish_document_delete('{doc}','{token}','{audit}');")
        for table in ('documents', 'document_chunks', 'document_index_revisions', 'document_revision_chunks'):
            column = 'id' if table == 'documents' else 'document_id'
            assert sql(f"select count(*) from {table} where {column}='{doc}';").strip() == '0'
        assert sql("select count(*) from documents where source='keep.txt';").strip() == '1'
        assert int(sql("select generation from answer_cache_generations where scope='corpus';")) > before
        audit_count = sql(f"select count(*) from audit_events where action='documents.delete' "
                          f"and resource_id='{doc}' and actor_user_id='{actor}';")
        assert audit_count.strip() == '1'
    finally:
        sql(f"select release_operation('documents:ingest','{token}');")


def test_active_jobs_block_and_stale_tokens_cannot_delete(deletion_database):
    sql = deletion_database
    doc, token = str(uuid4()), str(uuid4())
    sql(f"insert into documents(id,source,kind) values('{doc}','busy-delete.txt','txt');")
    sql(f"select claim_operation('documents:ingest','{token}');")
    try:
        for metadata in ('{"source":"busy-delete.txt","scope":"document"}', '{"scope":"corpus"}', '{}'):
            job = str(uuid4())
            sql(f"insert into ingest_jobs(id,kind,status,metadata) values('{job}','documents.ingest','queued','{metadata}');")
            assert json.loads(sql(f"select prepare_document_delete('{doc}','{token}');"))['status'] == 'busy'
            sql(f"delete from ingest_jobs where id='{job}';")
        with pytest.raises(subprocess.CalledProcessError):
            sql(f"select finish_document_delete('{doc}','{uuid4()}','{{}}');")
        # Audit insert failure rolls back deletion and cache changes together.
        with pytest.raises(subprocess.CalledProcessError):
            sql(f"select finish_document_delete('{doc}','{token}','{{\"actor_user_id\":\"invalid\"}}');")
        assert sql(f"select count(*) from documents where id='{doc}';").strip() == '1'
        for role in ('anon', 'authenticated'):
            with pytest.raises(subprocess.CalledProcessError):
                sql(f"set role {role}; select finish_document_delete('{doc}','{token}','{{}}');")
    finally:
        sql(f"select release_operation('documents:ingest','{token}');")
