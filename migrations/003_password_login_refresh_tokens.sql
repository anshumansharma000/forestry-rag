alter table app_users
add column if not exists password_hash text;

alter table app_users
add column if not exists must_change_password boolean not null default true;

alter table app_users
add column if not exists last_login_at timestamptz;

create index if not exists app_users_email_idx
on app_users (lower(email));

create table if not exists refresh_tokens (
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

create index if not exists refresh_tokens_user_created_idx
on refresh_tokens (user_id, created_at desc);

create index if not exists refresh_tokens_active_idx
on refresh_tokens (user_id, expires_at)
where revoked_at is null;
