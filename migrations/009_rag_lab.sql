create table if not exists rag_lab_experiments (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text,
  status text not null default 'draft' check (status in ('draft', 'building', 'ready', 'publishing', 'published', 'failed', 'archived')),
  owner_user_id uuid,
  config jsonb not null default '{}'::jsonb,
  published_revision_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists rag_lab_experiments_owner_updated_idx
on rag_lab_experiments (owner_user_id, updated_at desc);

create table if not exists rag_lab_files (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references rag_lab_experiments(id) on delete cascade,
  filename text not null,
  kind text not null,
  storage_key text not null unique,
  checksum_sha256 text not null,
  size_bytes bigint not null,
  extraction_key text,
  extraction_metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (experiment_id, filename)
);

create index if not exists rag_lab_files_experiment_idx
on rag_lab_files (experiment_id, created_at);

create table if not exists rag_lab_revisions (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references rag_lab_experiments(id) on delete cascade,
  revision_number integer not null,
  status text not null default 'queued' check (status in ('queued', 'building', 'ready', 'publishing', 'published', 'failed')),
  config jsonb not null,
  chunk_count integer not null default 0,
  error text,
  created_by uuid,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  published_at timestamptz,
  unique (experiment_id, revision_number)
);

alter table rag_lab_experiments
drop constraint if exists rag_lab_experiments_published_revision_id_fkey;

alter table rag_lab_experiments
add constraint rag_lab_experiments_published_revision_id_fkey
foreign key (published_revision_id) references rag_lab_revisions(id) on delete set null;

create index if not exists rag_lab_revisions_experiment_created_idx
on rag_lab_revisions (experiment_id, created_at desc);

create table if not exists rag_lab_chunks (
  id uuid primary key default gen_random_uuid(),
  revision_id uuid not null references rag_lab_revisions(id) on delete cascade,
  file_id uuid not null references rag_lab_files(id) on delete cascade,
  source text not null,
  chunk_index integer not null,
  chunk_type text not null default 'text',
  section_heading text,
  page_start integer,
  page_end integer,
  content text not null,
  token_estimate integer not null,
  metadata jsonb not null default '{}'::jsonb,
  embedding extensions.vector(768) not null,
  created_at timestamptz not null default now(),
  unique (revision_id, file_id, chunk_index)
);

create index if not exists rag_lab_chunks_revision_idx
on rag_lab_chunks (revision_id, file_id, chunk_index);

create index if not exists rag_lab_chunks_embedding_hnsw
on rag_lab_chunks using hnsw (embedding extensions.vector_cosine_ops);

create index if not exists rag_lab_chunks_fts_idx
on rag_lab_chunks using gin (
  to_tsvector('english', coalesce(source, '') || ' ' || coalesce(section_heading, '') || ' ' || content)
);

create table if not exists rag_lab_query_trials (
  id uuid primary key default gen_random_uuid(),
  revision_id uuid not null references rag_lab_revisions(id) on delete cascade,
  actor_user_id uuid,
  question text not null,
  retrieval_config jsonb not null,
  answer text,
  sources jsonb not null default '[]'::jsonb,
  confidence double precision,
  abstained boolean not null default false,
  latency_ms double precision,
  created_at timestamptz not null default now()
);

create index if not exists rag_lab_query_trials_revision_created_idx
on rag_lab_query_trials (revision_id, created_at desc);

create or replace function match_rag_lab_chunks (
  query_embedding extensions.vector(768),
  query_text text,
  match_count integer,
  target_revision_id uuid,
  vector_candidate_count integer default 50,
  text_candidate_count integer default 50
)
returns table (
  id uuid, document_id uuid, source text, chunk_index integer, chunk_type text,
  section_heading text, page_start integer, page_end integer, content text,
  metadata jsonb, similarity float
)
language sql stable
as $$
  with vector_matches as (
    select c.id,
      row_number() over (order by c.embedding <=> query_embedding) as vector_rank,
      greatest(0, 1 - (c.embedding <=> query_embedding)) as vector_similarity
    from rag_lab_chunks c
    where c.revision_id = target_revision_id
    order by c.embedding <=> query_embedding
    limit greatest(match_count, vector_candidate_count)
  ),
  text_matches as (
    select c.id,
      row_number() over (order by ts_rank_cd(
        to_tsvector('english', coalesce(c.source, '') || ' ' || coalesce(c.section_heading, '') || ' ' || c.content),
        websearch_to_tsquery('english', query_text)
      ) desc) as text_rank,
      ts_rank_cd(
        to_tsvector('english', coalesce(c.source, '') || ' ' || coalesce(c.section_heading, '') || ' ' || c.content),
        websearch_to_tsquery('english', query_text)
      ) as text_score
    from rag_lab_chunks c
    where c.revision_id = target_revision_id
      and nullif(trim(query_text), '') is not null
      and to_tsvector('english', coalesce(c.source, '') || ' ' || coalesce(c.section_heading, '') || ' ' || c.content)
          @@ websearch_to_tsquery('english', query_text)
    order by text_score desc
    limit greatest(match_count, text_candidate_count)
  ),
  candidates as (
    select id from vector_matches union select id from text_matches
  )
  select c.id, c.file_id as document_id, c.source, c.chunk_index, c.chunk_type,
    c.section_heading, c.page_start, c.page_end, c.content, c.metadata,
    ((0.75 * coalesce(vm.vector_similarity, 0))
      + (0.20 * coalesce(least(tm.text_score, 1), 0))
      + (0.05 * (coalesce(1.0 / (60 + vm.vector_rank), 0) + coalesce(1.0 / (60 + tm.text_rank), 0)) * 60))::float
      as similarity
  from candidates x
  join rag_lab_chunks c on c.id = x.id
  left join vector_matches vm on vm.id = c.id
  left join text_matches tm on tm.id = c.id
  order by similarity desc
  limit match_count;
$$;

create or replace function publish_rag_lab_revision(p_revision_id uuid, p_published_by uuid default null)
returns jsonb
language plpgsql
as $$
declare
  v_experiment rag_lab_experiments%rowtype;
  v_revision rag_lab_revisions%rowtype;
  v_file rag_lab_files%rowtype;
  v_document_id uuid;
  v_source text;
  v_documents integer := 0;
  v_chunks integer := 0;
  v_file_chunks integer;
begin
  select * into v_revision from rag_lab_revisions where id = p_revision_id for update;
  if not found then
    raise exception 'RAG Lab revision was not found';
  end if;
  if v_revision.status = 'published' then
    select count(distinct file_id), count(*) into v_documents, v_chunks
    from rag_lab_chunks where revision_id = p_revision_id;
    return jsonb_build_object(
      'documents', v_documents, 'chunks', v_chunks, 'revision_id', p_revision_id, 'already_published', true
    );
  end if;
  if v_revision.status <> 'ready' then
    raise exception 'RAG Lab revision is not ready to publish';
  end if;
  select * into v_experiment from rag_lab_experiments where id = v_revision.experiment_id for update;

  update rag_lab_revisions set status = 'publishing', updated_at = now() where id = p_revision_id;
  update rag_lab_experiments set status = 'publishing', updated_at = now() where id = v_experiment.id;

  for v_file in
    select f.*
    from rag_lab_files f
    where f.experiment_id = v_experiment.id
      and exists (
        select 1 from rag_lab_chunks c
        where c.revision_id = p_revision_id and c.file_id = f.id
      )
    order by f.created_at
  loop
    v_source := 'rag-lab/' || v_experiment.id::text || '/' || v_file.filename;
    insert into documents (source, kind, title, page_count, metadata)
    values (
      v_source,
      v_file.kind,
      coalesce(v_file.extraction_metadata->>'title', v_file.filename),
      nullif(v_file.extraction_metadata->>'page_count', '')::integer,
      coalesce(v_file.extraction_metadata->'document_metadata', '{}'::jsonb) || jsonb_build_object(
        'ingest_status', 'indexed', 'ingest_updated_at', now(), 'source_origin', 'rag_lab',
        'corpus', 'admin_curated', 'display_source', v_file.filename,
        'experiment_id', v_experiment.id, 'experiment_revision_id', v_revision.id,
        'experiment_revision', v_revision.revision_number, 'file_id', v_file.id,
        'file_checksum_sha256', v_file.checksum_sha256, 'rag_recipe', v_revision.config,
        'published_by', p_published_by, 'published_at', now()
      )
    )
    on conflict (source) do update set
      kind = excluded.kind, title = excluded.title, page_count = excluded.page_count,
      metadata = excluded.metadata, updated_at = now()
    returning id into v_document_id;

    delete from document_chunks where source = v_source;
    insert into document_chunks (
      document_id, source, chunk_index, chunk_type, section_heading, page_start, page_end,
      content, token_estimate, metadata, embedding
    )
    select v_document_id, v_source, c.chunk_index, c.chunk_type, c.section_heading, c.page_start, c.page_end,
      c.content, c.token_estimate,
      c.metadata || jsonb_build_object(
        'source_origin', 'rag_lab', 'corpus', 'admin_curated', 'display_source', v_file.filename,
        'experiment_id', v_experiment.id, 'experiment_revision_id', v_revision.id,
        'file_id', v_file.id, 'rag_recipe', v_revision.config
      ), c.embedding
    from rag_lab_chunks c
    where c.revision_id = p_revision_id and c.file_id = v_file.id;
    get diagnostics v_file_chunks = row_count;
    v_chunks := v_chunks + v_file_chunks;
    v_documents := v_documents + 1;
  end loop;

  update rag_lab_revisions
  set status = 'published', published_at = now(), updated_at = now(), error = null
  where id = p_revision_id;
  update rag_lab_experiments
  set status = 'published', published_revision_id = p_revision_id, updated_at = now()
  where id = v_experiment.id;

  return jsonb_build_object('documents', v_documents, 'chunks', v_chunks, 'revision_id', p_revision_id);
end;
$$;

-- Production retrieval must never see chunks from an indexing or failed document.
-- Apply the equivalent `documents` join to match_document_chunks in installations
-- that customized the function after migration 005.
