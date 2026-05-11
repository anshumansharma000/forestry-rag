drop function if exists match_document_chunks(extensions.vector, integer, jsonb);
drop function if exists match_document_chunks(extensions.vector, text, integer, jsonb, integer, integer);
drop table if exists chat_messages;
drop table if exists chat_sessions;
drop table if exists audit_events;
drop table if exists refresh_tokens;
drop table if exists document_chunks;
drop table if exists documents;
drop table if exists app_users;

create extension if not exists vector with schema extensions;

create table app_users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  full_name text,
  role text not null check (role in ('viewer', 'officer', 'knowledge_manager', 'admin')),
  password_hash text,
  must_change_password boolean not null default true,
  is_active boolean not null default true,
  last_login_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index app_users_email_idx
on app_users (lower(email));

create table refresh_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references app_users(id) on delete cascade,
  token_hash text not null unique,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  replaced_by uuid,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  ip_address text,
  user_agent text,
  metadata jsonb not null default '{}'::jsonb
);

create index refresh_tokens_user_created_idx
on refresh_tokens (user_id, created_at desc);

create index refresh_tokens_active_idx
on refresh_tokens (user_id, expires_at)
where revoked_at is null;

create table documents (
  id uuid primary key default gen_random_uuid(),
  source text not null unique,
  kind text not null,
  title text,
  page_count integer,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table document_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
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
  unique (source, chunk_index)
);

create index document_chunks_embedding_hnsw
on document_chunks
using hnsw (embedding extensions.vector_cosine_ops);

create index document_chunks_source_idx
on document_chunks (source);

create index document_chunks_fts_idx
on document_chunks
using gin (
  to_tsvector(
    'english',
    coalesce(source, '') || ' ' || coalesce(section_heading, '') || ' ' || content
  )
);

create table chat_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  title text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index chat_sessions_user_updated_idx
on chat_sessions (user_id, updated_at desc);

create table chat_messages (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references chat_sessions(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  sources jsonb not null default '[]'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index chat_messages_session_created_idx
on chat_messages (session_id, created_at);

create table audit_events (
  id uuid primary key default gen_random_uuid(),
  actor_user_id uuid,
  actor_email text,
  actor_role text,
  action text not null,
  resource_type text,
  resource_id text,
  ip_address text,
  user_agent text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index audit_events_actor_created_idx
on audit_events (actor_user_id, created_at desc);

create index audit_events_action_created_idx
on audit_events (action, created_at desc);

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
    where dc.metadata @> filter
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
    cross join search_query sq
    where sq.ts_query is not null
      and dc.metadata @> filter
      and to_tsvector('english', coalesce(dc.source, '') || ' ' || coalesce(dc.section_heading, '') || ' ' || dc.content) @@ sq.ts_query
    order by text_score desc
    limit greatest(match_count, text_candidate_count)
  ),
  candidates as (
    select id from vector_matches
    union
    select id from text_matches
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
