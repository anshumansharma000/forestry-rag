-- Additive transactional cache invalidation. No model calls or corpus reindex.
begin;
create table if not exists answer_cache_generations (
  scope text primary key,
  generation bigint not null default 0 check (generation >= 0)
);
alter table answer_cache_generations enable row level security;
revoke all on answer_cache_generations from public, anon, authenticated;
grant select, insert, update on answer_cache_generations to service_role;
insert into answer_cache_generations(scope) values ('corpus'), ('lab') on conflict do nothing;

create or replace function bump_answer_cache_generation() returns trigger
language plpgsql security invoker set search_path=public as $$
begin
  insert into answer_cache_generations(scope,generation) values (TG_ARGV[0],1)
  on conflict(scope) do update set generation=answer_cache_generations.generation+1;
  return null;
end; $$;

-- Live chunk mutations include both ordinary and Lab publication, direct edits,
-- deletes and truncation. Staged document_revision_chunks are intentionally absent.
drop trigger if exists answer_cache_chunks on document_chunks;
create trigger answer_cache_chunks after insert or update or delete or truncate on document_chunks
for each statement execute function bump_answer_cache_generation('corpus');

create or replace function bump_answer_document_generation() returns trigger
language plpgsql security invoker set search_path=public as $$
begin
  if (TG_OP <> 'INSERT' and OLD.metadata->>'ingest_status'='indexed') or
     (TG_OP <> 'DELETE' and NEW.metadata->>'ingest_status'='indexed') then
    update answer_cache_generations set generation=generation+1 where scope='corpus';
  end if;
  return null;
end; $$;
drop trigger if exists answer_cache_documents on documents;
create trigger answer_cache_documents after insert or update or delete on documents
for each row execute function bump_answer_document_generation();
drop trigger if exists answer_cache_documents_truncate on documents;
create trigger answer_cache_documents_truncate after truncate on documents
for each statement execute function bump_answer_cache_generation('corpus');

create or replace function bump_answer_registry_generation() returns trigger
language plpgsql security invoker set search_path=public as $$
begin
  if TG_OP <> 'INSERT' then
    insert into answer_cache_generations(scope,generation) values ('registry:'||OLD.namespace,1)
    on conflict(scope) do update set generation=answer_cache_generations.generation+1;
  end if;
  if TG_OP <> 'DELETE' and (TG_OP='INSERT' or NEW.namespace is distinct from OLD.namespace) then
    insert into answer_cache_generations(scope,generation) values ('registry:'||NEW.namespace,1)
    on conflict(scope) do update set generation=answer_cache_generations.generation+1;
  end if;
  return null;
end; $$;

do $optional$
begin
  if to_regclass('public.legal_document_profiles') is not null then
    execute 'drop trigger if exists answer_cache_registry on legal_document_profiles';
    execute 'create trigger answer_cache_registry after insert or update or delete on legal_document_profiles
             for each row execute function bump_answer_registry_generation()';
    -- Truncating the registry also invalidates every namespace through corpus epoch.
    execute 'drop trigger if exists answer_cache_registry_truncate on legal_document_profiles';
    execute 'create trigger answer_cache_registry_truncate after truncate on legal_document_profiles
             for each statement execute function bump_answer_cache_generation(''corpus'')';
  end if;
  if to_regclass('public.rag_lab_revisions') is not null then
    execute 'drop trigger if exists answer_cache_lab_revision on rag_lab_revisions';
    execute 'create trigger answer_cache_lab_revision after insert or update or delete or truncate on rag_lab_revisions
             for each statement execute function bump_answer_cache_generation(''lab'')';
    execute 'drop trigger if exists answer_cache_lab_chunks on rag_lab_chunks';
    execute 'create trigger answer_cache_lab_chunks after insert or update or delete or truncate on rag_lab_chunks
             for each statement execute function bump_answer_cache_generation(''lab'')';
  end if;
end; $optional$;

create or replace function answer_cache_versions(p_namespace text default '', p_revision uuid default null)
returns jsonb language plpgsql stable security invoker set search_path=public as $$
declare ready boolean; c bigint; r bigint;
begin
  if p_revision is not null then
    if to_regclass('public.rag_lab_revisions') is null then return null; end if;
    execute 'select exists(select 1 from rag_lab_revisions where id=$1 and status in (''ready'',''published''))'
      into ready using p_revision;
    if not ready then return null; end if;
    select generation into c from answer_cache_generations where scope='lab';
    return jsonb_build_object('corpus',c,'registry',0,'revision',p_revision);
  end if;
  select generation into c from answer_cache_generations where scope='corpus';
  select generation into r from answer_cache_generations where scope='registry:'||p_namespace;
  return jsonb_build_object('corpus',c,'registry',coalesce(r,0));
end; $$;

revoke all on function bump_answer_cache_generation(), bump_answer_document_generation(),
  bump_answer_registry_generation(), answer_cache_versions(text,uuid) from public,anon,authenticated;
grant execute on function bump_answer_cache_generation(), bump_answer_document_generation(),
  bump_answer_registry_generation(), answer_cache_versions(text,uuid) to service_role;
commit;
