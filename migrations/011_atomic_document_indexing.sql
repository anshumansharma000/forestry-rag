-- Apply before deploying the revision-based ingestion code. Existing published
-- chunks stay in place; new builds are isolated until transactional publication.
begin;
alter table documents add column if not exists index_generation bigint not null default 0;

create or replace function bump_document_index_generation() returns trigger
language plpgsql as $$
begin
  new.index_generation := old.index_generation + 1;
  return new;
end;
$$;
drop trigger if exists document_index_generation on documents;
create trigger document_index_generation before update on documents
for each row execute function bump_document_index_generation();

create table if not exists document_index_revisions (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  base_generation bigint not null,
  document_snapshot jsonb not null,
  status text not null default 'building' check (status in ('building', 'published', 'failed')),
  chunk_count integer,
  error text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create table if not exists document_revision_chunks (
  like document_chunks including defaults,
  revision_id uuid not null references document_index_revisions(id) on delete cascade,
  primary key (revision_id, chunk_index)
);
create index if not exists document_index_revisions_document_idx on document_index_revisions(document_id, created_at);
create index if not exists document_chunks_document_position_idx on document_chunks(document_id, chunk_index);

-- Only backend code uses these new staging objects. This does not change grants
-- or policies on existing application tables (the separate security audit).
alter table document_index_revisions enable row level security;
alter table document_revision_chunks enable row level security;
revoke all on document_index_revisions, document_revision_chunks from anon, authenticated;
grant select, insert, update, delete on document_index_revisions, document_revision_chunks to service_role;

create or replace function guard_document_revision() returns trigger
language plpgsql as $$
begin
  if new.id <> old.id or new.document_id <> old.document_id
     or new.base_generation <> old.base_generation or new.document_snapshot <> old.document_snapshot
     or new.created_at <> old.created_at or old.status <> 'building' then
    raise exception 'Revision snapshots and completed revisions are immutable';
  end if;
  return new;
end;
$$;
drop trigger if exists guard_document_revision on document_index_revisions;
create trigger guard_document_revision before update on document_index_revisions
for each row execute function guard_document_revision();

create or replace function guard_revision_chunk() returns trigger
language plpgsql as $$
declare r document_index_revisions%rowtype;
begin
  if tg_op = 'DELETE' and not exists (select 1 from document_index_revisions where id = old.revision_id) then
    return old; -- Allow explicit revision retention cleanup through its FK cascade.
  end if;
  if tg_op <> 'INSERT' then
    raise exception 'Revision chunks are immutable';
  end if;
  select * into r from document_index_revisions where id = new.revision_id for update;
  if not found or r.status <> 'building' then
    raise exception 'Revision is not building';
  end if;
  if new.document_id <> r.document_id or new.source <> r.document_snapshot->>'source' or new.chunk_index < 0 then
    raise exception 'Chunk does not belong to revision';
  end if;
  return new;
end;
$$;
drop trigger if exists guard_revision_chunk on document_revision_chunks;
create trigger guard_revision_chunk before insert or update or delete on document_revision_chunks
for each row execute function guard_revision_chunk();

create or replace function begin_document_revision(p_document jsonb) returns jsonb
language plpgsql security invoker set search_path = public, extensions as $$
declare d documents%rowtype; r document_index_revisions%rowtype;
begin
  if nullif(p_document->>'source', '') is null or nullif(p_document->>'kind', '') is null then
    raise exception 'Document source and kind are required';
  end if;
  insert into documents(source, kind, title, page_count, metadata)
  values(p_document->>'source', p_document->>'kind', p_document->>'title',
         (p_document->>'page_count')::integer, '{"ingest_status":"indexing"}'::jsonb)
  on conflict(source) do nothing;
  select * into d from documents where source = p_document->>'source' for update;
  insert into document_index_revisions(document_id, base_generation, document_snapshot)
  values(d.id, d.index_generation, p_document) returning * into r;
  return to_jsonb(r);
end;
$$;

create or replace function publish_document_revision(p_revision_id uuid, p_expected_chunks integer) returns jsonb
language plpgsql security invoker set search_path = public, extensions as $$
declare r document_index_revisions%rowtype; d documents%rowtype; n integer; first_index integer; last_index integer;
begin
  select * into r from document_index_revisions where id = p_revision_id;
  if not found then raise exception 'Revision not found'; end if;
  -- Always lock document before revision, including failure handling.
  select * into d from documents where id = r.document_id for update;
  select * into r from document_index_revisions where id = p_revision_id for update;
  if r.status = 'published' then
    return jsonb_build_object('revision_id', r.id, 'chunks', r.chunk_count, 'already_published', true);
  end if;
  if r.status <> 'building' then raise exception 'Revision is not building'; end if;
  if d.index_generation <> r.base_generation then raise exception 'Stale document revision; rebuild against current index'; end if;
  select count(*), min(chunk_index), max(chunk_index) into n, first_index, last_index
  from document_revision_chunks where revision_id = r.id;
  if p_expected_chunks is null or p_expected_chunks <= 0 or n <> p_expected_chunks or first_index <> 0 or last_index <> n - 1 then
    raise exception 'Revision is empty or incomplete';
  end if;
  delete from document_chunks where document_id = d.id;
  insert into document_chunks(id, document_id, source, chunk_index, chunk_type, section_heading,
                              page_start, page_end, content, token_estimate, metadata, embedding)
  select id, document_id, source, chunk_index, chunk_type, section_heading,
         page_start, page_end, content, token_estimate,
         metadata || jsonb_build_object('index_revision_id', r.id), embedding
  from document_revision_chunks where revision_id = r.id;
  update documents set kind = r.document_snapshot->>'kind', title = r.document_snapshot->>'title',
    page_count = (r.document_snapshot->>'page_count')::integer,
    metadata = coalesce(r.document_snapshot->'metadata', '{}'::jsonb) || jsonb_build_object(
      'ingest_status', 'indexed', 'ingest_updated_at', now(), 'chunks', n, 'index_revision_id', r.id),
    updated_at = now() where id = d.id;
  update document_index_revisions set status = 'published', chunk_count = n, finished_at = now(), error = null where id = r.id;
  return jsonb_build_object('revision_id', r.id, 'chunks', n);
end;
$$;

create or replace function record_document_ingest_failure(p_source text, p_error text) returns void
language plpgsql security invoker set search_path = public, extensions as $$
begin
  insert into documents(source, kind, title, metadata)
  values(p_source, coalesce(nullif(substring(p_source from '\.([^.]+)$'), ''), 'document'), p_source,
         jsonb_build_object('ingest_status', 'failed', 'ingest_error', p_error, 'ingest_updated_at', now()))
  on conflict(source) do update set
    metadata = documents.metadata || case when documents.metadata->>'ingest_status' = 'indexed'
      then jsonb_build_object('last_refresh_error', p_error, 'last_refresh_failed_at', now())
      else jsonb_build_object('ingest_status', 'failed', 'ingest_error', p_error, 'ingest_updated_at', now()) end,
    updated_at = now();
end;
$$;

create or replace function fail_document_revision(p_revision_id uuid, p_error text) returns void
language plpgsql security invoker set search_path = public, extensions as $$
declare r document_index_revisions%rowtype; d documents%rowtype;
begin
  select * into r from document_index_revisions where id = p_revision_id;
  if not found then return; end if;
  select * into d from documents where id = r.document_id for update;
  select * into r from document_index_revisions where id = p_revision_id for update;
  if r.status <> 'building' then return; end if;
  update document_index_revisions set status = 'failed', error = p_error, finished_at = now() where id = r.id;
  if d.index_generation = r.base_generation then
    perform record_document_ingest_failure(d.source, p_error);
  end if;
end;
$$;

create or replace function document_chunk_neighbors(
  p_document_id uuid, p_chunk_index integer, p_radius integer default 1, p_revision_id uuid default null
) returns table (
  id uuid, document_id uuid, source text, chunk_index integer, chunk_type text,
  section_heading text, page_start integer, page_end integer, content text,
  token_estimate integer, metadata jsonb
)
language sql stable security invoker set search_path = public, extensions as $$
  select c.id, c.document_id, c.source, c.chunk_index, c.chunk_type, c.section_heading,
         c.page_start, c.page_end, c.content, c.token_estimate, c.metadata
  from document_chunks c join documents d on d.id = c.document_id
  where c.document_id = p_document_id and d.metadata->>'ingest_status' = 'indexed'
    and c.chunk_index between greatest(0, p_chunk_index - p_radius) and p_chunk_index + p_radius
    and (p_revision_id is null or c.metadata->>'index_revision_id' = p_revision_id::text)
  order by c.chunk_index;
$$;

revoke all on function begin_document_revision(jsonb), publish_document_revision(uuid, integer),
  fail_document_revision(uuid, text), record_document_ingest_failure(text, text),
  document_chunk_neighbors(uuid, integer, integer, uuid) from public;
grant execute on function begin_document_revision(jsonb), publish_document_revision(uuid, integer),
  fail_document_revision(uuid, text), record_document_ingest_failure(text, text),
  document_chunk_neighbors(uuid, integer, integer, uuid) to service_role;

create or replace function match_document_chunks (
  query_embedding extensions.vector(768),
  query_text text default '',
  match_count integer default 5,
  filter jsonb default '{}'::jsonb,
  vector_candidate_count integer default 50,
  text_candidate_count integer default 50
)
returns table (
  id uuid,
  document_id uuid,
  source text,
  chunk_index integer,
  chunk_type text,
  section_heading text,
  page_start integer,
  page_end integer,
  content text,
  metadata jsonb,
  similarity float
)
language plpgsql
as $$
begin
  return query
  with search_input as (
    select
      nullif(trim(query_text), '') as raw_query,
      lower(coalesce(query_text, '')) as lower_query
  ),
  search_query as (
    select
      raw_query,
      lower_query,
      case
        when raw_query is null then null::tsquery
        else websearch_to_tsquery('english', raw_query)
      end as ts_query
    from search_input
  ),
  vector_matches as (
    select
      dc.id,
      row_number() over (order by dc.embedding <=> query_embedding) as vector_rank,
      greatest(0, 1 - (dc.embedding <=> query_embedding)) as vector_similarity
    from document_chunks dc
    join documents d on d.id = dc.document_id
    where d.metadata->>'ingest_status' = 'indexed' and dc.metadata @> filter
    order by dc.embedding <=> query_embedding
    limit greatest(match_count, vector_candidate_count)
  ),
  text_matches as (
    select
      dc.id,
      row_number() over (
        order by ts_rank_cd(
          to_tsvector('english', coalesce(dc.source, '') || ' ' || coalesce(dc.section_heading, '') || ' ' || dc.content),
          sq.ts_query
        ) desc
      ) as text_rank,
      ts_rank_cd(
        to_tsvector('english', coalesce(dc.source, '') || ' ' || coalesce(dc.section_heading, '') || ' ' || dc.content),
        sq.ts_query
      ) as text_score
    from document_chunks dc
    join documents d on d.id = dc.document_id
    cross join search_query sq
    where d.metadata->>'ingest_status' = 'indexed' and sq.ts_query is not null
      and dc.metadata @> filter
      and to_tsvector('english', coalesce(dc.source, '') || ' ' || coalesce(dc.section_heading, '') || ' ' || dc.content) @@ sq.ts_query
    order by text_score desc
    limit greatest(match_count, text_candidate_count)
  ),
  candidates as (
    select vm.id from vector_matches vm
    union
    select tm.id from text_matches tm
  ),
  scored as (
    select
      dc.id,
      dc.document_id,
      dc.source,
      dc.chunk_index,
      dc.chunk_type,
      dc.section_heading,
      dc.page_start,
      dc.page_end,
      dc.content,
      dc.metadata,
      coalesce(vm.vector_similarity, 0) as vector_similarity,
      coalesce(least(tm.text_score, 1), 0) as text_score,
      coalesce(1.0 / (60 + vm.vector_rank), 0) as vector_rrf,
      coalesce(1.0 / (60 + tm.text_rank), 0) as text_rrf,
      (
        case
          when sq.raw_query is not null
            and lower(dc.source) <> ''
            and sq.lower_query like '%' || lower(regexp_replace(dc.source, '\.[^.]+$', '')) || '%'
          then 0.08
          else 0
        end
        +
        case
          when sq.raw_query is not null
            and dc.section_heading is not null
            and length(dc.section_heading) >= 4
            and sq.lower_query like '%' || lower(dc.section_heading) || '%'
          then 0.05
          else 0
        end
        +
        case
          when dc.chunk_type = 'faq'
            and sq.lower_query ~ '\m(faq|question|answer)\M'
          then 0.03
          else 0
        end
        +
        case
          when dc.chunk_type = 'procedure'
            and sq.lower_query ~ '\m(process|procedure|workflow|step|steps|how)\M'
          then 0.03
          else 0
        end
      ) as metadata_boost
    from candidates c
    join document_chunks dc on dc.id = c.id
    left join vector_matches vm on vm.id = dc.id
    left join text_matches tm on tm.id = dc.id
    cross join search_query sq
  )
  select
    scored.id,
    scored.document_id,
    scored.source,
    scored.chunk_index,
    scored.chunk_type,
    scored.section_heading,
    scored.page_start,
    scored.page_end,
    scored.content,
    scored.metadata,
    (
      (0.70 * scored.vector_similarity)
      + (0.20 * scored.text_score)
      + (0.05 * (scored.vector_rrf + scored.text_rrf) * 60)
      + scored.metadata_boost
    )::float as similarity
  from scored
  order by similarity desc, vector_similarity desc, text_score desc
  limit match_count;
end;
$$;

do $optional$
begin
  if to_regclass('public.legal_document_profiles') is not null then
    execute $ddl$
create or replace function match_legal_chunks_v1(
  registry_namespace text,
  query_embedding extensions.vector(768),
  query_text text,
  instrument_types text[],
  match_count integer default 10,
  document_ids uuid[] default null
)
returns table (
  id uuid, document_id uuid, source text, chunk_index integer, chunk_type text,
  section_heading text, page_start integer, page_end integer, content text,
  metadata jsonb, similarity float
)
language sql stable security invoker set search_path = public, extensions
as $$
  select dc.id, dc.document_id, dc.source, dc.chunk_index, dc.chunk_type,
         dc.section_heading, dc.page_start, dc.page_end, dc.content, dc.metadata,
         (greatest(0, 1 - (dc.embedding <=> query_embedding)) +
          least(0.1, ts_rank_cd(to_tsvector('english', dc.content),
                               websearch_to_tsquery('english', query_text))))::float as similarity
  from document_chunks dc
  join documents d on d.id = dc.document_id
  left join legal_document_profiles lp
    on lp.document_id = dc.document_id and lp.namespace = registry_namespace
  where d.metadata->>'ingest_status' = 'indexed' and ((document_ids is not null and dc.document_id = any(document_ids))
     or (document_ids is null and lp.profile->>'reviewed' = 'true'
         and lp.profile->>'instrument_type' = any(instrument_types)))
  order by similarity desc, dc.id
  limit greatest(1, least(match_count, 200));
$$;

$ddl$;
  end if;
end;
$optional$;
commit;
