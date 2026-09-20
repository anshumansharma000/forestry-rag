-- Apply before deploying the security update. Existing tokens start at version 0.
begin;
alter table app_users add column if not exists token_version bigint not null default 0;
alter table refresh_tokens add column if not exists token_version bigint not null default 0;

-- Password invalidation is transactional even for existing administrative writers.
create or replace function invalidate_password_tokens() returns trigger
language plpgsql security invoker set search_path = public as $$
begin
  if new.password_hash is distinct from old.password_hash then
    new.token_version := old.token_version + 1;
    update refresh_tokens set revoked_at = coalesce(revoked_at, now())
    where user_id = old.id and revoked_at is null;
  else
    new.token_version := old.token_version;
  end if;
  return new;
end;
$$;
drop trigger if exists invalidate_password_tokens on app_users;
create trigger invalidate_password_tokens before update on app_users
for each row execute function invalidate_password_tokens();

create or replace function issue_auth_refresh_token(
  p_user_id uuid, p_version bigint, p_hash text, p_expires_at timestamptz,
  p_ip text default null, p_agent text default null, p_metadata jsonb default '{}'
) returns jsonb language plpgsql security invoker set search_path = public as $$
declare u app_users%rowtype; t refresh_tokens%rowtype;
begin
  select * into u from app_users where id = p_user_id for update;
  if not found or not u.is_active or u.token_version <> p_version then return null; end if;
  if p_expires_at <= clock_timestamp() or length(p_hash) <> 64 then raise exception 'Invalid token parameters'; end if;
  insert into refresh_tokens(user_id, token_version, token_hash, expires_at, ip_address, user_agent, metadata)
  values(u.id, u.token_version, p_hash, p_expires_at, p_ip, left(p_agent, 512), p_metadata) returning * into t;
  return jsonb_build_object('id', t.id, 'expires_at', t.expires_at);
end;
$$;

create or replace function rotate_auth_refresh_token(
  p_old_hash text, p_new_hash text, p_expires_at timestamptz, p_ip text default null, p_agent text default null
) returns jsonb language plpgsql security invoker set search_path = public as $$
declare t refresh_tokens%rowtype; u app_users%rowtype; replacement uuid;
begin
  select * into t from refresh_tokens where token_hash = p_old_hash;
  if not found then return null; end if;
  -- Shared lock order with password updates/issuance: user, then refresh token.
  select * into u from app_users where id = t.user_id for update;
  if not found or not u.is_active then return null; end if;
  select * into t from refresh_tokens where token_hash = p_old_hash for update;
  if not found or t.revoked_at is not null or t.expires_at <= clock_timestamp() or t.token_version <> u.token_version then
    return null;
  end if;
  if p_expires_at <= clock_timestamp() or length(p_new_hash) <> 64 then raise exception 'Invalid token parameters'; end if;
  insert into refresh_tokens(user_id, token_version, token_hash, expires_at, ip_address, user_agent, metadata)
  values(u.id, u.token_version, p_new_hash, p_expires_at, p_ip, left(p_agent, 512), '{"action":"refresh"}')
  returning id into replacement;
  update refresh_tokens set revoked_at = now(), last_used_at = now(), replaced_by = replacement where id = t.id;
  return jsonb_build_object('user', to_jsonb(u) - 'password_hash' - 'token_hash', 'expires_at', p_expires_at);
end;
$$;

create or replace function change_auth_password(
  p_user_id uuid, p_expected_hash text, p_new_hash text, p_must_change boolean default false
) returns jsonb language plpgsql security invoker set search_path = public as $$
declare u app_users%rowtype;
begin
  select * into u from app_users where id = p_user_id for update;
  if not found or (p_expected_hash is not null and (not u.is_active or u.password_hash is distinct from p_expected_hash)) then
    return null;
  end if;
  update app_users set password_hash = p_new_hash, must_change_password = p_must_change, updated_at = now()
  where id = p_user_id returning * into u;
  return to_jsonb(u) - 'password_hash' - 'token_hash';
end;
$$;

revoke all on function issue_auth_refresh_token(uuid, bigint, text, timestamptz, text, text, jsonb),
  rotate_auth_refresh_token(text, text, timestamptz, text, text),
  change_auth_password(uuid, text, text, boolean) from public, anon, authenticated;
grant execute on function issue_auth_refresh_token(uuid, bigint, text, timestamptz, text, text, jsonb),
  rotate_auth_refresh_token(text, text, timestamptz, text, text),
  change_auth_password(uuid, text, text, boolean) to service_role;
notify pgrst, 'reload schema';
commit;
