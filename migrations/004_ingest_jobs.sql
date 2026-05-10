create table if not exists ingest_jobs (
  id uuid primary key default gen_random_uuid(),
  kind text not null,
  status text not null check (status in ('queued', 'running', 'succeeded', 'failed')),
  actor_user_id uuid,
  result jsonb,
  error text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create index if not exists ingest_jobs_status_created_idx
on ingest_jobs (status, created_at desc);

create index if not exists ingest_jobs_actor_created_idx
on ingest_jobs (actor_user_id, created_at desc);
