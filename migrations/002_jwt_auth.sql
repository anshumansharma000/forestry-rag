alter table app_users
drop constraint if exists app_users_token_hash_key;

do $$
begin
  if exists (
    select 1
    from information_schema.columns
    where table_name = 'app_users'
      and column_name = 'token_hash'
  ) then
    alter table app_users alter column token_hash drop not null;
  end if;
end $$;
