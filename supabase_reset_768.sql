drop function if exists match_document_chunks(extensions.vector, integer, jsonb);
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
  match_count integer default 5,
  filter jsonb default '{}'::jsonb
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
    1 - (dc.embedding <=> query_embedding) as similarity
  from document_chunks dc
  where dc.metadata @> filter
  order by dc.embedding <=> query_embedding
  limit match_count;
end;
$$;
