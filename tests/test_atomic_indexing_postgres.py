"""Opt-in real database tests: RAG_POSTGRES_TESTS=1 python -m pytest -q tests/test_atomic_indexing_postgres.py.

Requires Docker and pgvector/pgvector:pg16. Creates and removes an isolated,
network-disabled container; never connects to the application's Supabase URL.
"""
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(os.getenv('RAG_POSTGRES_TESTS') != '1', reason='opt-in disposable Docker PostgreSQL tests')
ROOT = Path(__file__).resolve().parents[1]


def command(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout


@pytest.fixture(scope='module')
def database():
    container = 'rag-index-test-' + uuid4().hex[:12]
    command('docker', 'run', '-d', '--rm', '--name', container, '--network', 'none',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', 'pgvector/pgvector:pg16')
    def sql(statement):
        return command('docker', 'exec', '-i', container, 'psql', '-X', '-qAt', '-U', 'postgres',
                       '-v', 'ON_ERROR_STOP=1', input=statement)
    try:
        for attempt in range(60):
            try:
                sql('select 1;')
                break
            except subprocess.CalledProcessError:
                if attempt == 59:
                    raise
                time.sleep(0.25)
        sql('create schema extensions; create role anon; create role authenticated; '
            'create role service_role bypassrls; alter database postgres set search_path = public, extensions;')
        for path in ('supabase_schema.sql', 'migrations/009_rag_lab.sql', 'migrations/010_legal_registry.sql',
                     'migrations/011_atomic_document_indexing.sql'):
            sql((ROOT / path).read_text())
        # Existing backend grants are outside this migration's security-audit scope.
        sql('grant select, insert, update, delete on documents, document_chunks to service_role; '
            'grant usage on schema extensions to service_role;')
        yield container, sql
    finally:
        command('docker', 'rm', '-f', container)


def test_transactional_publication_and_all_retrieval_paths(database):
    _, sql = database
    sql((ROOT / 'tests/sql/atomic_document_indexing.sql').read_text())


def stage(sql, revision, content):
    sql(f"""insert into document_revision_chunks(revision_id, document_id, source, chunk_index,
        chunk_type, content, token_estimate, embedding)
        values('{revision['id']}', '{revision['document_id']}', 'race.txt', 0, 'section',
               '{content}', 10, array_fill(0.01::real, array[768])::extensions.vector);""")


def test_concurrent_publication_never_exposes_partial_index(database):
    container, sql = database
    a = json.loads(sql("select begin_document_revision('{\"source\":\"race.txt\",\"kind\":\"txt\"}');"))
    stage(sql, a, 'old')
    sql(f"select publish_document_revision('{a['id']}', 1);")
    b = json.loads(sql("select begin_document_revision('{\"source\":\"race.txt\",\"kind\":\"txt\"}');"))
    c = json.loads(sql("select begin_document_revision('{\"source\":\"race.txt\",\"kind\":\"txt\"}');"))
    stage(sql, b, 'new')
    stage(sql, c, 'stale')
    # Hold an actual publishing transaction open while another session reads and publishes.
    process = subprocess.Popen(['docker', 'exec', '-i', container, 'psql', '-X', '-qAt', '-U', 'postgres',
                                '-v', 'ON_ERROR_STOP=1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(f"begin; select publish_document_revision('{b['id']}', 1); select 'READY';\n")
        process.stdin.flush()
        assert process.stdout.readline().strip().startswith('{')
        assert process.stdout.readline().strip() == 'READY'
        assert sql("select content from document_chunks where source='race.txt';").strip() == 'old'
        # Competing publisher cannot acquire the document lock during publication.
        with pytest.raises(subprocess.CalledProcessError) as error:
            sql(f"set lock_timeout='150ms'; select publish_document_revision('{c['id']}', 1);")
        assert 'lock timeout' in error.value.stderr
        process.stdin.write('commit;\n')
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert sql("select content from document_chunks where source='race.txt';").strip() == 'new'
        with pytest.raises(subprocess.CalledProcessError) as error:
            sql(f"select publish_document_revision('{c['id']}', 1);")
        assert 'Stale document revision' in error.value.stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_new_rpc_works_as_backend_role(database):
    _, sql = database
    revision = json.loads(sql("set role service_role; select begin_document_revision('{\"source\":\"role.txt\",\"kind\":\"txt\"}');"))
    sql(f"""set role service_role;
        insert into document_revision_chunks(revision_id, document_id, source, chunk_index, content, token_estimate, embedding)
        values('{revision['id']}', '{revision['document_id']}', 'role.txt', 0, 'role evidence', 10,
               array_fill(0.01::real, array[768])::extensions.vector);
        select publish_document_revision('{revision['id']}', 1);""")
    assert sql("select content from document_chunks where source='role.txt';").strip() == 'role evidence'


def test_fresh_install_and_optional_registry_order(database):
    _, sql = database
    sql('create database no_registry;')
    prefix = "\\connect no_registry\nset search_path=public,extensions;\n"
    sql(prefix + 'create schema extensions;\n' + (ROOT / 'supabase_schema.sql').read_text())
    migration = (ROOT / 'migrations/011_atomic_document_indexing.sql').read_text()
    sql(prefix + migration)
    sql(prefix + migration)  # Reapplication must be safe.
    sql(prefix + (ROOT / 'migrations/010_legal_registry.sql').read_text())
    assert sql(prefix + "select count(*) from document_chunk_neighbors(gen_random_uuid(), 0);").strip() == '0'
