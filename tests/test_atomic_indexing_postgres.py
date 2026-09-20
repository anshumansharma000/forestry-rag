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
        # TCP becomes available only after Docker's temporary initialization server exits.
        return command('docker', 'exec', '-i', container, 'psql', '-h', '127.0.0.1', '-X', '-qAt', '-U', 'postgres',
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
                     'migrations/011_atomic_document_indexing.sql', 'migrations/012_auth_security.sql', 'migrations/013_consistency.sql'):
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


def test_atomic_refresh_rotation_password_invalidation_and_permissions(database):
    from concurrent.futures import ThreadPoolExecutor
    _, sql = database
    sql('grant select, insert, update on app_users, refresh_tokens to service_role;')
    user_id = str(uuid4())
    sql(f"insert into app_users(id,email,role,password_hash) values('{user_id}','{user_id}@test.org','viewer','old');")
    old = 'a' * 64
    issued = sql(f"set role service_role; select issue_auth_refresh_token('{user_id}',0,'{old}',now()+interval '1 day');")
    assert json.loads(issued)['id']
    def rotate(i):
        return sql(f"set role service_role; select rotate_auth_refresh_token('{old}','{i:064x}',now()+interval '1 day');").strip()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(rotate, range(1,9)))
    winners = [json.loads(value) for value in results if value]
    assert len(winners) == 1
    assert 'password_hash' not in winners[0]['user']
    assert sql(f"select count(*) from refresh_tokens where user_id='{user_id}' and revoked_at is null;").strip() == '1'
    successor = sql(f"select token_hash from refresh_tokens where user_id='{user_id}' and revoked_at is null;").strip()
    changed = json.loads(sql(f"set role service_role; select change_auth_password('{user_id}','old','new',false);"))
    assert changed['token_version'] == 1
    assert sql(f"select rotate_auth_refresh_token('{successor}','{'b'*64}',now()+interval '1 day');").strip() == ''
    assert sql(f"select issue_auth_refresh_token('{user_id}',0,'{'c'*64}',now()+interval '1 day');").strip() == ''
    assert sql(f"select change_auth_password('{user_id}','old','stale',false);").strip() == ''
    assert sql(f"select password_hash from app_users where id='{user_id}';").strip() == 'new'
    # Admin resets need no old password and invalidate every token again.
    assert json.loads(sql(f"select change_auth_password('{user_id}',null,'reset',true);"))['token_version'] == 2
    sql(f"update app_users set full_name='Profile edit' where id='{user_id}';")
    assert sql(f"select token_version from app_users where id='{user_id}';").strip() == '2'
    for role in ('anon', 'authenticated'):
        with pytest.raises(subprocess.CalledProcessError) as denied:
            sql(f"set role {role}; select change_auth_password('{user_id}',null,'attack',false);")
        assert 'permission denied' in denied.value.stderr
    sql('grant execute on function change_auth_password(uuid,text,text,boolean) to anon, authenticated;')
    sql((ROOT / 'migrations/012_auth_security.sql').read_text())
    assert sql("select has_function_privilege('anon','change_auth_password(uuid,text,text,boolean)','EXECUTE');").strip() == 'f'
    assert sql(f"select token_version from app_users where id='{user_id}';").strip() == '2'


def test_rotation_rolls_back_if_replacement_insert_fails(database):
    _, sql = database
    user_id = str(uuid4())
    sql(f"insert into app_users(id,email,role,password_hash) values('{user_id}','{user_id}@test.org','viewer','old');")
    old = 'd' * 64
    sql(f"select issue_auth_refresh_token('{user_id}',0,'{old}',now()+interval '1 day');")
    with pytest.raises(subprocess.CalledProcessError):
        sql(f"select rotate_auth_refresh_token('{old}','{old}',now()+interval '1 day');")
    assert sql(f"select count(*) from refresh_tokens where token_hash='{old}' and revoked_at is null;").strip() == '1'


def test_consistent_ingestion_skips_same_fingerprint_and_fences_expired_writer(database):
    _, sql = database
    token, replacement = str(uuid4()), str(uuid4())
    assert sql(f"select claim_operation('documents:ingest','{token}');").strip() == 't'
    assert sql(f"select claim_operation('documents:ingest','{replacement}');").strip() == 'f'
    document = json.dumps({'source': 'consistent.txt', 'kind': 'txt', 'metadata': {'content_fingerprint': 'version-a'}})
    revision = json.loads(sql(f"select begin_consistent_revision('{document}','{token}');"))
    def insert(r, text):
        sql(f"""insert into document_revision_chunks(revision_id,document_id,source,chunk_index,content,token_estimate,embedding)
            values('{r['id']}','{r['document_id']}','consistent.txt',0,'{text}',10,
                   array_fill(0.01::real,array[768])::extensions.vector);""")
    insert(revision, 'version-a')
    sql(f"select publish_consistent_revision('{revision['id']}',1,'{token}');")
    again = json.loads(sql(f"select begin_consistent_revision('{document}','{token}');"))
    assert again['already_indexed'] is True
    changed = document.replace('version-a', 'version-b')
    stale = json.loads(sql(f"select begin_consistent_revision('{changed}','{token}');"))
    insert(stale, 'stale')
    sql("update operation_leases set expires_at=now()-interval '1 second' where name='documents:ingest';")
    assert sql(f"select claim_operation('documents:ingest','{replacement}');").strip() == 't'
    with pytest.raises(subprocess.CalledProcessError) as rejected:
        sql(f"select publish_consistent_revision('{stale['id']}',1,'{token}');")
    assert 'ownership expired' in rejected.value.stderr
    assert sql("select content from document_chunks where source='consistent.txt';").strip() == 'version-a'
    current = json.loads(sql(f"select begin_consistent_revision('{changed}','{replacement}');"))
    insert(current, 'version-b')
    sql(f"select publish_consistent_revision('{current['id']}',1,'{replacement}');")
    # Releasing an old token must never remove its successor's lease.
    sql(f"select release_operation('documents:ingest','{token}');")
    assert sql("select token from operation_leases where name='documents:ingest';").strip() == replacement
    sql(f"select release_operation('documents:ingest','{replacement}');")


def test_chat_turns_are_atomic_replayable_owner_scoped_and_serialized(database):
    from concurrent.futures import ThreadPoolExecutor
    _, sql = database
    user, session = str(uuid4()), str(uuid4())
    request_id = str(uuid4())
    sql(f"insert into chat_sessions(id,user_id) values('{session}','{user}');")
    payload = json.dumps({'message': 'Question', 'top_k': None})
    def claim(i):
        token = str(uuid4())
        result = json.loads(sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{payload}','{token}');"))
        return token, result
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    winners = [(token, result) for token, result in claims if result['state'] == 'claimed']
    assert len(winners) == 1
    token = winners[0][0]
    assert sql(f"select count(*) from chat_messages where session_id='{session}';").strip() == '0'
    denied = json.loads(sql(f"select begin_chat_turn('{session}','{uuid4()}','{request_id}','{payload}','{uuid4()}');"))
    assert denied['state'] == 'not_found'
    different = json.loads(sql(f"select begin_chat_turn('{session}','{user}','another','{payload}','{uuid4()}');"))
    assert different['state'] == 'busy'
    # Assistant insertion fails; the user insertion must roll back with it.
    with pytest.raises(subprocess.CalledProcessError):
        sql(f"select complete_chat_turn('{session}','{user}','{request_id}','{token}','{{}}');")
    assert sql(f"select count(*) from chat_messages where session_id='{session}';").strip() == '0'
    response = json.dumps({'session_id': session, 'answer': 'Evidence [1].', 'sources': [], 'outcome': 'answered',
                           'abstained': False, 'confidence': .8, 'search_query': 'Question'})
    completed = json.loads(sql(f"select complete_chat_turn('{session}','{user}','{request_id}','{token}','{response}');"))
    assert completed['user_message']['content'] == 'Question'
    assert completed['assistant_message']['metadata']['outcome'] == 'answered'
    sql(f"select release_operation('chat:{session}','{token}');")
    replay = json.loads(sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{payload}','{uuid4()}');"))
    assert replay == {'state': 'completed', 'response': completed}
    assert sql(f"select count(*) from chat_messages where session_id='{session}';").strip() == '2'
    conflict = json.loads(sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{{}}','{uuid4()}');"))
    assert conflict['state'] == 'conflict'
    sql(f"delete from chat_messages where id='{completed['assistant_message']['id']}';")
    gone = json.loads(sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{payload}','{uuid4()}');"))
    assert gone['state'] == 'gone'


def test_chat_recovery_fences_old_completion(database):
    _, sql = database
    user, session, request_id, old, new = [str(uuid4()) for _ in range(5)]
    sql(f"insert into chat_sessions(id,user_id) values('{session}','{user}');")
    payload = '{"message":"Recover this"}'
    sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{payload}','{old}');")
    sql(f"update operation_leases set expires_at=now()-interval '1 second' where name='chat:{session}';")
    result = json.loads(sql(f"select begin_chat_turn('{session}','{user}','{request_id}','{payload}','{new}');"))
    assert result['state'] == 'claimed'
    with pytest.raises(subprocess.CalledProcessError):
        sql(f"select complete_chat_turn('{session}','{user}','{request_id}','{old}','{{}}');")
    assert sql(f"select count(*) from chat_messages where session_id='{session}';").strip() == '0'


def test_job_recovery_preserves_live_jobs_and_terminal_results(database):
    _, sql = database
    job, token, new_token = [str(uuid4()) for _ in range(3)]
    sql(f"insert into ingest_jobs(id,kind,status) values('{job}','documents.ingest','queued');")
    sql(f"select claim_operation('job:{job}','{token}');")
    sql(f"select transition_ingest_job('{job}','running','{token}');")
    # Late dispatch callback cannot regress a running job.
    assert json.loads(sql(f"select transition_ingest_job('{job}','queued');"))['status'] == 'running'
    sql(f"update ingest_jobs set updated_at=now()-interval '10 minutes' where id='{job}';")
    assert sql(f"select count(*) from recover_ingest_jobs() where id='{job}';").strip() == '0'
    sql(f"update operation_leases set expires_at=now()-interval '1 second' where name='job:{job}';")
    assert sql(f"select count(*) from recover_ingest_jobs() where id='{job}';").strip() == '1'
    assert sql(f"select count(*) from recover_ingest_jobs() where id='{job}';").strip() == '0'
    sql(f"select claim_operation('job:{job}','{new_token}');")
    with pytest.raises(subprocess.CalledProcessError):
        sql(f"select transition_ingest_job('{job}','succeeded','{token}');")
    sql(f"select transition_ingest_job('{job}','running','{new_token}');")
    sql(f"select transition_ingest_job('{job}','succeeded','{new_token}','{{\"chunks\":4}}');")
    sql(f"select transition_ingest_job('{job}','queued');")
    terminal = json.loads(sql(f"select transition_ingest_job('{job}','failed','{new_token}');"))
    assert terminal['status'] == 'succeeded'
    assert terminal['result'] == {'chunks': 4}
    assert terminal['attempt_count'] == 2


def test_consistency_migration_reapplication_and_rpc_permissions(database):
    _, sql = database
    sql((ROOT / 'migrations/013_consistency.sql').read_text())
    for role in ('anon', 'authenticated'):
        for function in ('claim_operation(text,uuid)', 'begin_consistent_revision(jsonb,uuid)',
                         'begin_chat_turn(uuid,uuid,text,jsonb,uuid)', 'recover_ingest_jobs()'):
            assert sql(f"select has_function_privilege('{role}','{function}','EXECUTE');").strip() == 'f'


def test_lab_creation_build_retry_and_fenced_publication(database):
    _, sql = database
    experiment, file_id, actor, token = [str(uuid4()) for _ in range(4)]
    sql(f"insert into rag_lab_experiments(id,name,owner_user_id,config) values('{experiment}','Consistency','{actor}','{{}}');")
    sql(f"""insert into rag_lab_files(id,experiment_id,filename,kind,storage_key,checksum_sha256,size_bytes)
        values('{file_id}','{experiment}','rules.txt','txt','test/rules.txt','checksum',10);""")
    created = json.loads(sql(f"select create_consistent_lab_revision('{experiment}',null,'{actor}');"))
    assert created['state'] == 'created'
    assert created['revision']['file_ids'] == [file_id]
    job, revision = created['job']['id'], created['revision']['id']
    assert json.loads(sql(f"select create_consistent_lab_revision('{experiment}',null,'{actor}');"))['state'] == 'busy'
    assert sql(f"select count(*) from ingest_jobs where metadata->>'experiment_id'='{experiment}';").strip() == '1'
    sql(f"select claim_operation('job:{job}','{token}'); select claim_operation('lab:{revision}','{token}');")
    def mutate(action, payload):
        return sql(f"select mutate_lab_revision('{job}','{token}','{revision}','{action}','{json.dumps(payload)}');")
    mutate('status', {'status': 'building'})
    row = {'file_id': file_id, 'source': 'rules.txt', 'chunk_index': 0, 'chunk_type': 'text', 'content': 'Evidence',
           'token_estimate': 2, 'metadata': {}, 'embedding': [.01] * 768}
    mutate('chunks', {'rows': [row]})
    mutate('chunks', {'rows': [row]})
    assert sql(f"select count(*) from rag_lab_chunks where revision_id='{revision}';").strip() == '1'
    mutate('status', {'status': 'ready', 'chunk_count': 1})
    result = json.loads(mutate('publish', {'published_by': actor}))
    assert result['chunks'] == 1
    assert json.loads(mutate('publish', {'published_by': actor}))['already_published'] is True
    sql(f"update operation_leases set expires_at=now()-interval '1 second' where name='job:{job}';")
    with pytest.raises(subprocess.CalledProcessError):
        mutate('status', {'status': 'failed'})
    assert sql(f"select status from rag_lab_revisions where id='{revision}';").strip() == 'published'


def test_job_recovery_respects_backoff_and_attempt_budget(database):
    _, sql = database
    delayed, exhausted = str(uuid4()), str(uuid4())
    sql(f"""insert into ingest_jobs(id,kind,status,available_at,updated_at,attempt_count)
        values('{delayed}','documents.ingest','queued',now()+interval '1 hour',now()-interval '1 hour',1),
              ('{exhausted}','documents.ingest','running',now()-interval '1 hour',now()-interval '1 hour',5);""")
    assert sql(f"select count(*) from recover_ingest_jobs() where id in ('{delayed}','{exhausted}');").strip() == '0'
    assert sql(f"select status from ingest_jobs where id='{delayed}';").strip() == 'queued'
    assert sql(f"select status from ingest_jobs where id='{exhausted}';").strip() == 'failed'
