create table if not exists app_users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  full_name text,
  role text not null check (role in ('viewer', 'officer', 'knowledge_manager', 'admin')),
  token_hash text not null unique,
  is_active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table chat_sessions
add column if not exists user_id uuid;

create index if not exists chat_sessions_user_updated_idx
on chat_sessions (user_id, updated_at desc);

create table if not exists audit_events (
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

create index if not exists audit_events_actor_created_idx
on audit_events (actor_user_id, created_at desc);

create index if not exists audit_events_action_created_idx
on audit_events (action, created_at desc);
