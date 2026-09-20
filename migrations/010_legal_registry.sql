-- Additive sidecar only. No changes to documents, chunks, embeddings or existing RPCs.
-- Apply as a separate reviewed deployment step; application startup never runs this.
begin;
create table if not exists legal_document_profiles (
  namespace text not null check (length(namespace) between 1 and 64),
  document_id uuid not null references documents(id) on delete cascade,
  profile jsonb not null default '{}'::jsonb check (jsonb_typeof(profile) = 'object'),
  reviewer_id text,
  updated_at timestamptz not null default now(),
  primary key (namespace, document_id)
);
alter table legal_document_profiles enable row level security;
revoke all on legal_document_profiles from anon, authenticated;
grant select, insert, update, delete on legal_document_profiles to service_role;

-- New name and signature: old binaries continue to use match_document_chunks unchanged.
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
revoke all on function match_legal_chunks_v1(text, extensions.vector, text, text[], integer, uuid[]) from public;
grant execute on function match_legal_chunks_v1(text, extensions.vector, text, text[], integer, uuid[]) to service_role;
commit;
