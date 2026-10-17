"""Additive migration/invalidations on the existing disposable pgvector fixture."""

import json
import os
import subprocess
from uuid import uuid4

import pytest

from tests.test_atomic_indexing_postgres import ROOT, database  # noqa: F401

pytestmark = pytest.mark.skipif(os.getenv("RAG_POSTGRES_TESTS") != "1", reason="opt-in disposable PostgreSQL tests")


@pytest.fixture(scope="module")
def upgraded(database):  # noqa: F811
    container, sql = database
    for _ in range(2):
        for migration in ("014_answer_cache_versions.sql", "015_answer_query_helpers.sql"):
            sql((ROOT / "migrations" / migration).read_text())
    return container, sql


def indexed(sql, name):
    revision = json.loads(sql('select begin_document_revision(\'{"source":"' + name + '","kind":"txt"}\');'))
    for i in range(3):
        sql(f"""insert into document_revision_chunks(revision_id,document_id,source,chunk_index,chunk_type,
             content,token_estimate,embedding) values('{revision['id']}','{revision['document_id']}','{name}',
             {i},'section','Fee {100+i}',10,array_fill(0.01::real,array[768])::extensions.vector);""")
    sql(f"select publish_document_revision('{revision['id']}',3);")
    return revision


def version(sql, namespace=""):
    return json.loads(sql(f"select answer_cache_versions('{namespace}');"))


def test_staging_is_invisible_and_publication_invalidates_atomically(upgraded):
    _, sql = upgraded
    before = version(sql)
    r = json.loads(sql('select begin_document_revision(\'{"source":"cache-stage.txt","kind":"txt"}\');'))
    sql(f"""insert into document_revision_chunks(revision_id,document_id,source,chunk_index,content,token_estimate,embedding)
         values('{r['id']}','{r['document_id']}','cache-stage.txt',0,'Fee 100',10,
                array_fill(0.01::real,array[768])::extensions.vector);""")
    assert version(sql) == before
    sql(f"select publish_document_revision('{r['id']}',1);")
    assert version(sql)["corpus"] > before["corpus"]
    after = version(sql)
    sql(f"begin; update document_chunks set content='Fee 999' where document_id='{r['document_id']}'; rollback;")
    assert version(sql) == after
    sql(f"update document_chunks set content='Fee 200' where document_id='{r['document_id']}';")
    assert version(sql)["corpus"] > after["corpus"]


def test_namespace_change_and_delete_invalidate_both_scopes(upgraded):
    _, sql = upgraded
    r = indexed(sql, "cache-registry.txt")
    initial_a, initial_b = version(sql, "cache-a"), version(sql, "cache-b")
    sql(f"insert into legal_document_profiles(namespace,document_id,profile) values('cache-a','{r['document_id']}','{{}}');")
    assert version(sql, "cache-a")["registry"] > initial_a["registry"]
    first_a = version(sql, "cache-a")
    sql(f"update legal_document_profiles set namespace='cache-b' where document_id='{r['document_id']}';")
    assert version(sql, "cache-a")["registry"] > first_a["registry"]
    assert version(sql, "cache-b")["registry"] > initial_b["registry"]
    last_b = version(sql, "cache-b")
    sql(f"delete from documents where id='{r['document_id']}';")
    assert version(sql, "cache-b")["registry"] > last_b["registry"]
    assert version(sql)["corpus"] > last_b["corpus"]


def test_batch_neighbors_preserves_anchor_revision_and_backend_role(upgraded):
    _, sql = upgraded
    r = indexed(sql, "cache-neighbors.txt")
    payload = json.dumps([{"anchor_id": "a", "document_id": r["document_id"], "chunk_index": 1, "revision_id": r["id"]}])
    rows = json.loads(
        sql("set role service_role; select coalesce(jsonb_agg(to_jsonb(n)),'[]') " f"from document_chunk_neighbors_batch('{payload}') n;")
    )
    assert [v["chunk_index"] for v in rows] == [0, 1, 2]
    assert all(v["anchor_id"] == "a" for v in rows)
    payload = json.dumps([{"anchor_id": "a", "document_id": r["document_id"], "chunk_index": 1, "revision_id": str(uuid4())}])
    assert sql(f"select count(*) from document_chunk_neighbors_batch('{payload}');").strip() == "0"
    with pytest.raises(subprocess.CalledProcessError):
        sql("set role anon; select answer_cache_versions();")
    with pytest.raises(subprocess.CalledProcessError):
        sql("set role authenticated; select * from document_chunk_neighbors_batch('[]');")


def test_lab_generation_does_not_change_live_corpus(upgraded):
    _, sql = upgraded
    before = version(sql)
    eid, rid = str(uuid4()), str(uuid4())
    sql(f"insert into rag_lab_experiments(id,name) values('{eid}','cache-test');")
    sql(f"insert into rag_lab_revisions(id,experiment_id,revision_number,status,config) values('{rid}','{eid}',1,'building','{{}}');")
    assert sql(f"select answer_cache_versions('', '{rid}') is null;").strip() == "t"
    assert version(sql) == before
    sql(f"update rag_lab_revisions set status='ready' where id='{rid}';")
    ready = json.loads(sql(f"select answer_cache_versions('', '{rid}');"))
    assert ready["revision"] == rid
    sql(f"delete from rag_lab_revisions where id='{rid}';")
    assert sql(f"select answer_cache_versions('', '{rid}') is null;").strip() == "t"
    assert version(sql) == before


def test_recent_messages_have_deterministic_latest_order(upgraded):
    _, sql = upgraded
    session = str(uuid4())
    sql(f"insert into chat_sessions(id,title) values('{session}','ordering');")
    sql(f"""insert into chat_messages(session_id,role,content,created_at)
        select '{session}','user',i::text,'2026-01-01'::timestamptz+make_interval(secs=>i)
        from generate_series(1,14) i;""")
    values = json.loads(
        sql(f"""select jsonb_agg(content order by created_at,id) from
        (select * from chat_messages where session_id='{session}' order by created_at desc,id desc limit 10) m;""")
    )
    assert values == [str(i) for i in range(5, 15)]
