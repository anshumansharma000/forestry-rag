-- Run only against a disposable database after schema and migrations 009–011.
begin;
create function pg_temp.assert_true(ok boolean, message text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'Assertion failed: %', message; end if; end;
$$;
create function pg_temp.stage(r jsonb, p_position integer, body text) returns void language sql as $$
  insert into document_revision_chunks(revision_id, document_id, source, chunk_index, chunk_type, content, token_estimate, metadata, embedding)
  values((r->>'id')::uuid, (r->>'document_id')::uuid, r->'document_snapshot'->>'source', p_position,
         'section', body, 10, '{}', array_fill(0.01::real, array[768])::extensions.vector);
$$;
create function pg_temp.reject_test_chunk() returns trigger language plpgsql as $$
begin raise exception 'Synthetic active insert failure'; end;
$$;
create trigger test_reject_chunk before insert on document_chunks
for each row when (new.content = 'Replacement fee 200') execute function pg_temp.reject_test_chunk();
do $$
declare a jsonb; b jsonb; c jsonb; initial jsonb; result jsonb; doc uuid; old_chunk uuid; n integer;
        e uuid; f uuid; lab uuid; fresh jsonb;
begin
  a := begin_document_revision('{"source":"atomic-test.txt","kind":"txt","title":"Original","metadata":{"index_version":"4"}}');
  doc := (a->>'document_id')::uuid;
  perform pg_temp.stage(a, 0, 'Original rule fee 100');
  perform pg_temp.assert_true((select count(*) = 0 from document_chunks where document_id = doc), 'staging must not be active');
  perform publish_document_revision((a->>'id')::uuid, 1);
  initial := a;
  select id into old_chunk from document_chunks where document_id = doc;
  perform pg_temp.assert_true((select metadata->>'ingest_status' = 'indexed' from documents where id = doc), 'published status');

  a := begin_document_revision('{"source":"atomic-test.txt","kind":"txt","title":"Replacement","metadata":{}}');
  perform pg_temp.stage(a, 0, 'Replacement fee 200');
  perform pg_temp.assert_true((select content = 'Original rule fee 100' from document_chunks where document_id = doc), 'old index survives staging');
  perform pg_temp.assert_true((select title = 'Original' from documents where id = doc), 'old metadata survives staging');
  begin
    perform publish_document_revision((a->>'id')::uuid, 2);
    raise exception 'Expected incomplete publication rejection';
  exception when raise_exception then
    if sqlerrm <> 'Revision is empty or incomplete' then raise; end if;
  end;
  perform pg_temp.assert_true((select id = old_chunk from document_chunks where document_id = doc), 'failed publication rolls back');
  begin
    perform publish_document_revision((a->>'id')::uuid, 1);
    raise exception 'Expected active insert failure';
  exception when raise_exception then
    if sqlerrm <> 'Synthetic active insert failure' then raise; end if;
  end;
  perform pg_temp.assert_true((select id = old_chunk from document_chunks where document_id = doc), 'insertion error restores deleted old chunks');
  perform fail_document_revision((a->>'id')::uuid, 'synthetic failure');
  perform pg_temp.assert_true((select metadata->>'ingest_status' = 'indexed' from documents where id = doc), 'failed refresh preserves availability');
  perform pg_temp.assert_true((select content = 'Original rule fee 100' from document_chunks where document_id = doc), 'failed refresh preserves content');

  a := begin_document_revision('{"source":"atomic-test.txt","kind":"txt","title":"Winner","metadata":{}}');
  b := begin_document_revision('{"source":"atomic-test.txt","kind":"txt","title":"Stale","metadata":{}}');
  perform pg_temp.stage(a, 0, 'Winning fee 300');
  perform pg_temp.stage(b, 0, 'Stale fee 400');
  perform publish_document_revision((a->>'id')::uuid, 1);
  result := publish_document_revision((a->>'id')::uuid, 1);
  perform pg_temp.assert_true((result->>'already_published')::boolean, 'publication retry idempotent');
  perform fail_document_revision((a->>'id')::uuid, 'timeout after commit');
  perform pg_temp.assert_true((select status = 'published' from document_index_revisions where id = (a->>'id')::uuid), 'ambiguous timeout cannot undo publication');
  begin
    perform publish_document_revision((b->>'id')::uuid, 1);
    raise exception 'Expected stale rejection';
  exception when raise_exception then
    if sqlerrm <> 'Stale document revision; rebuild against current index' then raise; end if;
  end;
  perform fail_document_revision((b->>'id')::uuid, 'stale');
  perform pg_temp.assert_true((select content = 'Winning fee 300' from document_chunks where document_id = doc), 'stale build cannot overwrite winner');
  begin
    perform pg_temp.stage(a, 1, 'late write');
    raise exception 'Expected frozen revision';
  exception when raise_exception then
    if sqlerrm <> 'Revision is not building' then raise; end if;
  end;
  begin
    update document_revision_chunks set content = 'mutation' where revision_id = (a->>'id')::uuid;
    raise exception 'Expected immutable chunks';
  exception when raise_exception then
    if sqlerrm <> 'Revision chunks are immutable' then raise; end if;
  end;
  select count(*) into n from document_chunk_neighbors(doc, 0, 1, (initial->>'id')::uuid);
  perform pg_temp.assert_true(n = 0, 'neighbors cannot cross revisions');
  select count(*) into n from document_chunk_neighbors(doc, 0, 1, (a->>'id')::uuid);
  perform pg_temp.assert_true(n = 1, 'current neighbors available');

  -- Failed/new documents and legacy leftover chunks are excluded everywhere.
  fresh := begin_document_revision('{"source":"failed-test.txt","kind":"txt","metadata":{}}');
  perform pg_temp.stage(fresh, 0, 'Invisible failed evidence');
  perform fail_document_revision((fresh->>'id')::uuid, 'failed');
  insert into document_chunks(document_id, source, chunk_index, chunk_type, content, token_estimate, metadata, embedding)
  values((fresh->>'document_id')::uuid, 'failed-test.txt', 0, 'section', 'Invisible failed evidence', 10, '{}',
         array_fill(0.01::real, array[768])::extensions.vector);
  select count(*) into n from match_document_chunks(array_fill(0.01::real, array[768])::extensions.vector, 'evidence', 100);
  perform pg_temp.assert_true(n = 1, 'failed documents excluded from hybrid retrieval');
  select count(*) into n from document_chunk_neighbors((fresh->>'document_id')::uuid, 0);
  perform pg_temp.assert_true(n = 0, 'failed documents excluded from neighbors');
  select count(*) into n from match_legal_chunks_v1('test', array_fill(0.01::real, array[768])::extensions.vector,
    'evidence', '{}', 100, array[(fresh->>'document_id')::uuid]);
  perform pg_temp.assert_true(n = 0, 'failed documents excluded from legal retrieval');
  update documents set metadata = '{"ingest_status":"indexing"}' where id = (fresh->>'document_id')::uuid;
  select count(*) into n from match_document_chunks(array_fill(0.01::real, array[768])::extensions.vector, 'evidence', 100);
  perform pg_temp.assert_true(n = 1, 'indexing documents excluded');

  -- Lab publication remains transactional and invalidates an older normal build.
  insert into rag_lab_experiments(name) values('atomic lab test') returning id into e;
  insert into rag_lab_files(experiment_id, filename, kind, storage_key, checksum_sha256, size_bytes)
  values(e, 'lab.txt', 'txt', 'test/' || e::text, 'test', 1) returning id into f;
  insert into rag_lab_revisions(experiment_id, revision_number, status, config)
  values(e, 1, 'ready', '{}') returning id into lab;
  insert into rag_lab_chunks(revision_id, file_id, source, chunk_index, content, token_estimate, embedding)
  values(lab, f, 'lab.txt', 0, 'Lab evidence', 10, array_fill(0.01::real, array[768])::extensions.vector);
  c := begin_document_revision(jsonb_build_object('source', 'rag-lab/' || e::text || '/lab.txt', 'kind', 'txt', 'metadata', '{}'::jsonb));
  perform pg_temp.stage(c, 0, 'Competing normal build');
  perform publish_rag_lab_revision(lab);
  begin
    perform publish_document_revision((c->>'id')::uuid, 1);
    raise exception 'Expected stale Lab conflict';
  exception when raise_exception then
    if sqlerrm <> 'Stale document revision; rebuild against current index' then raise; end if;
  end;
  perform pg_temp.assert_true((select content = 'Lab evidence' from document_chunks where document_id = (c->>'document_id')::uuid), 'Lab publication preserved');
end;
$$;
rollback;
