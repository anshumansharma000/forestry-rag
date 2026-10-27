-- Requires migrations 011, 013 and 014. Apply before deploying deletion.
begin;
do $$
declare missing text[] := '{}';
begin
  if to_regclass('public.document_index_revisions') is null then
    missing := array_append(missing, '011_atomic_document_indexing.sql');
  end if;
  if to_regprocedure('public.assert_operation(text,uuid)') is null then
    missing := array_append(missing, '013_consistency.sql');
  end if;
  if to_regprocedure('public.answer_cache_versions(text,uuid)') is null then
    missing := array_append(missing, '014_answer_cache_versions.sql');
  end if;
  if cardinality(missing) > 0 then
    raise exception 'Missing prerequisite migrations: %', array_to_string(missing, ', ');
  end if;
end; $$;
create or replace function prepare_document_delete(p_id uuid, p_token uuid) returns jsonb
language plpgsql security invoker set search_path=public as $$
declare d documents%rowtype;
begin
  perform assert_operation('documents:ingest',p_token);
  select * into d from documents where id=p_id for update;
  if not found then return jsonb_build_object('status','missing'); end if;
  if exists(select 1 from ingest_jobs where kind='documents.ingest' and status in ('queued','running')
    and (metadata->>'source'=d.source or metadata->>'scope'='corpus'
         or nullif(metadata->>'source','') is null)) then
    return jsonb_build_object('status','busy');
  end if;
  return jsonb_build_object('status','ready','source',d.source);
end; $$;

create or replace function finish_document_delete(p_id uuid, p_token uuid, p_audit jsonb) returns void
language plpgsql security invoker set search_path=public as $$
declare d documents%rowtype;
begin
  perform assert_operation('documents:ingest',p_token);
  select * into d from documents where id=p_id for update;
  if not found then return; end if;
  -- Cascades remove live chunks, immutable revisions, staged chunks and any
  -- legal profiles. Migration 014 invalidates retrieval and answer caches.
  delete from documents where id=p_id;
  insert into audit_events(actor_user_id,actor_email,actor_role,action,resource_type,resource_id,
                           ip_address,user_agent,metadata)
  values((p_audit->>'actor_user_id')::uuid,p_audit->>'actor_email',p_audit->>'actor_role',
         'documents.delete','document',p_id::text,p_audit->>'ip_address',p_audit->>'user_agent',
         jsonb_build_object('source',d.source,'storage_deleted',true));
end; $$;
revoke all on function prepare_document_delete(uuid,uuid),finish_document_delete(uuid,uuid,jsonb)
  from public,anon,authenticated;
grant execute on function prepare_document_delete(uuid,uuid),finish_document_delete(uuid,uuid,jsonb) to service_role;
notify pgrst, 'reload schema';
commit;
