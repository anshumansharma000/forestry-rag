begin;
-- Durable ownership survives Redis loss. Tokens fence stale processes at commit.
create table if not exists operation_leases (
  name text primary key,
  token uuid not null,
  expires_at timestamptz not null
);
alter table operation_leases enable row level security;
revoke all on operation_leases from public, anon, authenticated;
grant select, insert, update, delete on operation_leases to service_role;

create or replace function claim_operation(p_name text, p_token uuid) returns boolean
language plpgsql security invoker set search_path=public as $$
begin
  insert into operation_leases values(p_name,p_token,clock_timestamp()+interval '5 minutes')
  on conflict(name) do update set token=excluded.token, expires_at=excluded.expires_at
    where operation_leases.expires_at <= clock_timestamp() or operation_leases.token=p_token;
  return found;
end; $$;
create or replace function renew_operation(p_name text, p_token uuid) returns boolean
language plpgsql security invoker set search_path=public as $$
begin
  update operation_leases set expires_at=clock_timestamp()+interval '5 minutes'
  where name=p_name and token=p_token and expires_at>clock_timestamp();
  return found;
end; $$;
create or replace function release_operation(p_name text, p_token uuid) returns void
language sql security invoker set search_path=public as $$
  delete from operation_leases where name=p_name and token=p_token;
$$;
create or replace function assert_operation(p_name text, p_token uuid) returns void
language plpgsql security invoker set search_path=public as $$
begin
  perform 1 from operation_leases where name=p_name and token=p_token and expires_at>clock_timestamp() for update;
  if not found then raise exception 'Operation ownership expired' using errcode='40001'; end if;
end; $$;

-- A version fingerprint is stored only when publication commits successfully.
create or replace function begin_consistent_revision(p_document jsonb, p_token uuid) returns jsonb
language plpgsql security invoker set search_path=public,extensions as $$
declare d documents%rowtype; result jsonb;
begin
  perform assert_operation('documents:ingest',p_token);
  select * into d from documents where source=p_document->>'source' for update;
  if found and d.metadata->>'ingest_status'='indexed'
     and d.metadata->>'content_fingerprint'=p_document->'metadata'->>'content_fingerprint' then
    return jsonb_build_object('already_indexed',true,'document_id',d.id);
  end if;
  result:=begin_document_revision(p_document || jsonb_build_object('_operation_token',p_token));
  return result;
end; $$;
create or replace function publish_consistent_revision(p_revision_id uuid,p_expected_chunks integer,p_token uuid)
returns jsonb language plpgsql security invoker set search_path=public,extensions as $$
begin
  perform assert_operation('documents:ingest',p_token);
  return publish_document_revision(p_revision_id,p_expected_chunks);
end; $$;
create or replace function fail_consistent_revision(p_revision_id uuid,p_error text,p_token uuid)
returns void language plpgsql security invoker set search_path=public,extensions as $$
begin
  perform assert_operation('documents:ingest',p_token);
  perform fail_document_revision(p_revision_id,p_error);
end; $$;
create or replace function record_consistent_ingest_failure(p_source text,p_error text,p_token uuid)
returns void language plpgsql security invoker set search_path=public,extensions as $$
begin
  perform assert_operation('documents:ingest',p_token);
  perform record_document_ingest_failure(p_source,p_error);
end; $$;
revoke all on function publish_consistent_revision(uuid,integer,uuid),fail_consistent_revision(uuid,text,uuid),
  record_consistent_ingest_failure(text,text,uuid) from public,anon,authenticated;
grant execute on function publish_consistent_revision(uuid,integer,uuid),fail_consistent_revision(uuid,text,uuid),
  record_consistent_ingest_failure(text,text,uuid) to service_role;

create or replace function fence_document_revision() returns trigger
language plpgsql security invoker set search_path=public as $$
begin
  if old.document_snapshot ? '_operation_token' then
    perform assert_operation('documents:ingest',
                             (old.document_snapshot->>'_operation_token')::uuid);
  end if;
  return new;
end; $$;
drop trigger if exists fence_document_revision on document_index_revisions;
create trigger fence_document_revision before update on document_index_revisions
for each row execute function fence_document_revision();

alter table ingest_jobs add column if not exists available_at timestamptz not null default now();
alter table ingest_jobs add column if not exists attempt_count integer not null default 0;
create or replace function transition_ingest_job(
  p_id uuid,p_status text,p_token uuid default null,p_result jsonb default null,p_error text default null,p_metadata jsonb default '{}'
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare j ingest_jobs%rowtype;
begin
  -- Every worker transition is fenced. Dispatch updates may only annotate queued jobs.
  if p_token is not null then perform assert_operation('job:'||p_id::text,p_token); end if;
  select * into j from ingest_jobs where id=p_id for update;
  if not found then return null; end if;
  if j.status in ('succeeded','failed') then return to_jsonb(j); end if;
  if p_token is null and (p_status <> 'queued' or j.status <> 'queued') then return to_jsonb(j); end if;
  update ingest_jobs set status=p_status,
    result=case when p_status='succeeded' then p_result else result end,
    error=p_error, metadata=metadata||coalesce(p_metadata,'{}'),updated_at=clock_timestamp(),
    available_at=case when p_token is not null and p_status='queued' then clock_timestamp()+
      make_interval(secs=>greatest(0,coalesce((p_metadata->>'retry_delay_seconds')::integer,5))) else available_at end,
    attempt_count=greatest(0,attempt_count+case when p_status='running' then 1
      when p_token is not null and p_status='queued' and j.status='running' and p_metadata->>'capacity_wait'='true' then -1 else 0 end),
    started_at=case when p_status='running' then clock_timestamp() else started_at end,
    finished_at=case when p_status in ('succeeded','failed') then clock_timestamp() else null end
  where id=p_id returning * into j;
  return to_jsonb(j);
end; $$;
create or replace function recover_ingest_jobs() returns setof ingest_jobs
language plpgsql security invoker set search_path=public as $$
declare j ingest_jobs%rowtype;
begin
  for j in select * from ingest_jobs i where i.status in ('queued','running')
    and i.updated_at < clock_timestamp()-interval '90 seconds' and i.available_at<=clock_timestamp()
    and not exists(select 1 from operation_leases l where l.name='job:'||i.id::text and l.expires_at>clock_timestamp())
    order by i.updated_at for update skip locked limit 50
  loop
    update ingest_jobs set status=case when attempt_count>=5 then 'failed' else 'queued' end,
      error=case when attempt_count>=5 then 'Recovery attempt limit reached.' else error end,
      finished_at=case when attempt_count>=5 then clock_timestamp() else null end,
      metadata=metadata||jsonb_build_object('recovery_count',coalesce((metadata->>'recovery_count')::int,0)+1),
      updated_at=clock_timestamp() where id=j.id returning * into j;
    if j.status='queued' then return next j;
    elsif j.kind='rag_lab.build' and not exists(select 1 from operation_leases
      where name='lab:'||(j.metadata->>'revision_id') and expires_at>clock_timestamp()) then
      update rag_lab_revisions set status='failed',error=j.error,updated_at=clock_timestamp()
        where id=(j.metadata->>'revision_id')::uuid and status in ('queued','building','failed');
      if found then
        update rag_lab_experiments set status='failed',updated_at=clock_timestamp()
          where id=(j.metadata->>'experiment_id')::uuid and status='building';
      end if;
    end if;
  end loop;
end; $$;

create table if not exists chat_turns (
  session_id uuid not null references chat_sessions(id) on delete cascade,
  request_id text not null,
  request jsonb not null,
  token uuid not null,
  response jsonb,
  created_at timestamptz not null default now(),
  primary key(session_id,request_id)
);
alter table chat_turns enable row level security;
revoke all on chat_turns from public,anon,authenticated;
grant select,insert,update,delete on chat_turns to service_role;
create or replace function tombstone_chat_turn() returns trigger
language plpgsql security invoker set search_path=public as $$
begin
  update chat_turns set response='{"deleted":true}' where session_id=old.session_id
    and request_id=old.metadata->>'request_id';
  return old;
end; $$;
drop trigger if exists tombstone_chat_turn on chat_messages;
create trigger tombstone_chat_turn after delete on chat_messages for each row execute function tombstone_chat_turn();

create or replace function begin_chat_turn(p_session uuid,p_user uuid,p_request_id text,p_request jsonb,p_token uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare t chat_turns%rowtype;
begin
  perform 1 from chat_sessions where id=p_session and user_id=p_user for update;
  if not found then return jsonb_build_object('state','not_found'); end if;
  select * into t from chat_turns where session_id=p_session and request_id=p_request_id;
  if found then
    if t.request <> p_request then return jsonb_build_object('state','conflict'); end if;
    if t.response->>'deleted'='true' then return jsonb_build_object('state','gone'); end if;
    if t.response is not null then return jsonb_build_object('state','completed','response',t.response); end if;
  end if;
  if not claim_operation('chat:'||p_session::text,p_token) then return jsonb_build_object('state','busy'); end if;
  insert into chat_turns(session_id,request_id,request,token) values(p_session,p_request_id,p_request,p_token)
  on conflict(session_id,request_id) do update set token=excluded.token;
  return jsonb_build_object('state','claimed');
end; $$;
create or replace function complete_chat_turn(p_session uuid,p_user uuid,p_request_id text,p_token uuid,p_response jsonb)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare t chat_turns%rowtype; u chat_messages%rowtype; a chat_messages%rowtype; result jsonb;
begin
  perform 1 from chat_sessions where id=p_session and user_id=p_user for update;
  if not found then raise exception 'Chat session not found'; end if;
  perform assert_operation('chat:'||p_session::text,p_token);
  select * into t from chat_turns where session_id=p_session and request_id=p_request_id for update;
  if not found or t.token<>p_token then raise exception 'Chat turn ownership expired'; end if;
  if t.response is not null then return t.response; end if;
  insert into chat_messages(session_id,role,content,metadata)
  values(p_session,'user',t.request->>'message',jsonb_build_object('request_id',p_request_id)) returning * into u;
  insert into chat_messages(session_id,role,content,sources,metadata,created_at)
  values(p_session,'assistant',p_response->>'answer',p_response->'sources',
    jsonb_build_object('request_id',p_request_id,'search_query',p_response->>'search_query',
      'outcome',p_response->>'outcome','abstained',p_response->'abstained','retrieval_confidence',p_response->'confidence'),
    u.created_at+interval '1 microsecond') returning * into a;
  result:=p_response||jsonb_build_object('user_message',to_jsonb(u),'assistant_message',to_jsonb(a),'request_id',p_request_id);
  update chat_turns set response=result where session_id=p_session and request_id=p_request_id;
  update chat_sessions set updated_at=clock_timestamp() where id=p_session;
  return result;
end; $$;

revoke all on function claim_operation(text,uuid),renew_operation(text,uuid),release_operation(text,uuid),assert_operation(text,uuid),
  begin_consistent_revision(jsonb,uuid),transition_ingest_job(uuid,text,uuid,jsonb,text,jsonb),recover_ingest_jobs(),
  begin_chat_turn(uuid,uuid,text,jsonb,uuid),complete_chat_turn(uuid,uuid,text,uuid,jsonb) from public,anon,authenticated;
grant execute on function claim_operation(text,uuid),renew_operation(text,uuid),release_operation(text,uuid),assert_operation(text,uuid),
  begin_consistent_revision(jsonb,uuid),transition_ingest_job(uuid,text,uuid,jsonb,text,jsonb),recover_ingest_jobs(),
  begin_chat_turn(uuid,uuid,text,jsonb,uuid),complete_chat_turn(uuid,uuid,text,uuid,jsonb) to service_role;
create or replace function mutate_lab_revision(p_job uuid,p_token uuid,p_revision uuid,p_action text,p_payload jsonb)
returns jsonb language plpgsql security invoker set search_path=public,extensions as $$
declare r rag_lab_revisions%rowtype; published_number integer;
begin
  perform assert_operation('job:'||p_job::text,p_token);
  perform assert_operation('lab:'||p_revision::text,p_token);
  select * into r from rag_lab_revisions where id=p_revision for update;
  if not found then raise exception 'Revision not found'; end if;
  if p_action='publish' then
    perform 1 from rag_lab_experiments where id=r.experiment_id for update;
    select published.revision_number into published_number from rag_lab_experiments e
      join rag_lab_revisions published on published.id=e.published_revision_id where e.id=r.experiment_id;
    if r.status<>'published' and published_number>r.revision_number then
      raise exception 'A newer experiment revision is already published';
    end if;
    return publish_rag_lab_revision(p_revision,(p_payload->>'published_by')::uuid);
  end if;
  if r.status in ('ready','published') then return to_jsonb(r); end if;
  if p_action='status' then
    update rag_lab_revisions set status=p_payload->>'status',
      chunk_count=coalesce((p_payload->>'chunk_count')::integer,chunk_count),
      error=p_payload->>'error',updated_at=clock_timestamp() where id=p_revision;
    update rag_lab_experiments set status=p_payload->>'status',updated_at=clock_timestamp()
      where id=r.experiment_id and status<>'archived'
        and r.revision_number=(select max(revision_number) from rag_lab_revisions where experiment_id=r.experiment_id);
  elsif p_action='chunks' then
    if r.status<>'building' then raise exception 'Revision is not building'; end if;
    insert into rag_lab_chunks(revision_id,file_id,source,chunk_index,chunk_type,section_heading,page_start,page_end,
                               content,token_estimate,metadata,embedding)
    select p_revision,(x->>'file_id')::uuid,x->>'source',(x->>'chunk_index')::integer,x->>'chunk_type',
      x->>'section_heading',(x->>'page_start')::integer,(x->>'page_end')::integer,x->>'content',
      (x->>'token_estimate')::integer,x->'metadata',(x->'embedding')::text::extensions.vector
    from jsonb_array_elements(p_payload->'rows') x
    on conflict(revision_id,file_id,chunk_index) do nothing;
  else raise exception 'Unsupported revision operation'; end if;
  return jsonb_build_object('ok',true);
end; $$;
revoke all on function mutate_lab_revision(uuid,uuid,uuid,text,jsonb) from public,anon,authenticated;
grant execute on function mutate_lab_revision(uuid,uuid,uuid,text,jsonb) to service_role;

alter table rag_lab_revisions add column if not exists file_ids jsonb;

create or replace function create_consistent_lab_revision(p_experiment uuid,p_config jsonb,p_actor uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare e rag_lab_experiments%rowtype; r rag_lab_revisions%rowtype; j ingest_jobs%rowtype; n integer;
begin
  select * into e from rag_lab_experiments where id=p_experiment for update;
  if not found then return jsonb_build_object('state','not_found'); end if;
  if e.status in ('building','publishing','archived') then return jsonb_build_object('state','busy'); end if;
  if not exists(select 1 from rag_lab_files where experiment_id=p_experiment) then
    return jsonb_build_object('state','empty');
  end if;
  select coalesce(max(revision_number),0)+1 into n from rag_lab_revisions where experiment_id=p_experiment;
  insert into rag_lab_revisions(experiment_id,revision_number,status,config,created_by,file_ids)
    values(p_experiment,n,'queued',coalesce(p_config,e.config),p_actor,
      (select jsonb_agg(id order by created_at,id) from rag_lab_files where experiment_id=p_experiment)) returning * into r;
  insert into ingest_jobs(kind,status,actor_user_id,metadata)
    values('rag_lab.build','queued',p_actor,jsonb_build_object('revision_id',r.id,'experiment_id',p_experiment)) returning * into j;
  update rag_lab_experiments set status='building',config=r.config,updated_at=clock_timestamp() where id=p_experiment;
  return jsonb_build_object('state','created','revision',to_jsonb(r),'job',to_jsonb(j));
end; $$;
revoke all on function create_consistent_lab_revision(uuid,jsonb,uuid) from public,anon,authenticated;
grant execute on function create_consistent_lab_revision(uuid,jsonb,uuid) to service_role;

create or replace function guard_lab_file_insert() returns trigger
language plpgsql security invoker set search_path=public as $$
declare state text;
begin
  select status into state from rag_lab_experiments where id=new.experiment_id for update;
  if state in ('building','publishing','archived') then raise exception 'Experiment is not accepting files'; end if;
  return new;
end; $$;
drop trigger if exists guard_lab_file_insert on rag_lab_files;
create trigger guard_lab_file_insert before insert on rag_lab_files for each row execute function guard_lab_file_insert();

alter table rag_lab_query_trials add column if not exists outcome text;
notify pgrst,'reload schema';
commit;
