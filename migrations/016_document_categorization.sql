-- Apply before clients submit upload categorization. Existing uploads/search RPCs
-- remain valid without it. No embeddings are changed or documents deleted.
begin;

create or replace function document_categorization_ready() returns boolean
language sql stable security invoker set search_path=public as $$ select true; $$;

create or replace function save_document_categorization(
  p_source text, p_patch jsonb, p_updated_by text, p_namespace text default null
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare d documents%rowtype; c jsonb; v jsonb; overrides jsonb;
begin
  if nullif(trim(p_source),'') is null or p_source ~ '[/\\]' or jsonb_typeof(p_patch) <> 'object' then
    raise exception 'A filename and categorization object are required';
  end if;
  if p_namespace is not null and (p_namespace !~ '^[[:alnum:]_-]{1,64}$') then
    raise exception 'Invalid legal registry namespace';
  end if;
  insert into documents(source,kind,title,metadata)
  values(p_source,coalesce(nullif(substring(p_source from '\.([^.]+)$'),''),'document'),p_source,
         '{"ingest_status":"uploaded"}'::jsonb)
  on conflict(source) do nothing;
  select * into d from documents where source=p_source for update;
  c := coalesce(d.metadata->'categorization','{}'::jsonb);
  overrides := coalesce(c->'overrides','{}'::jsonb) || p_patch;
  v := coalesce(c->'values','{}'::jsonb) || p_patch;
  c := c || jsonb_build_object('version',1,'values',v,'overrides',overrides,
       'updated_by',p_updated_by,
       'field_sources',coalesce(c->'field_sources','{}'::jsonb) ||
          coalesce((select jsonb_object_agg(key,'manual') from jsonb_object_keys(p_patch) key),'{}'::jsonb),
       'review_required',(v->>'document_type' is null or exists(select 1 from jsonb_object_keys(v) key where not overrides ? key)),
       'missing_fields',(select coalesce(jsonb_agg(key),'[]'::jsonb) from unnest(array[
          'document_type','subjects','issuing_authority','jurisdiction','state','issued_date',
          'effective_date','consolidation_as_of','identifiers']) key
          where v->key is null or v->key='null'::jsonb or v->key='[]'::jsonb));
  -- Namespace is supplied by server configuration, never by upload JSON.
  c := (c - 'registry_namespace') || case when p_namespace is not null
    then jsonb_build_object('registry_namespace',p_namespace) else '{}'::jsonb end;
  update documents set metadata=coalesce(metadata,'{}'::jsonb) || jsonb_build_object('categorization',c) ||
      case when p_patch ? 'issued_date' then jsonb_build_object('issued_date',p_patch->'issued_date') else '{}'::jsonb end ||
      case when p_patch ? 'effective_date' then jsonb_build_object('effective_date',p_patch->'effective_date') else '{}'::jsonb end ||
      case when p_patch ? 'issuing_authority' then jsonb_build_object('authority',p_patch->'issuing_authority') else '{}'::jsonb end,
      updated_at=now() where id=d.id;
  -- Corrections to published metadata take effect without regenerating vectors.
  -- Immutable historical revision snapshots remain untouched.
  if p_patch ?| array['issued_date','effective_date','issuing_authority'] then
    update document_chunks set metadata=coalesce(metadata,'{}'::jsonb) ||
      case when p_patch ? 'issued_date' then jsonb_build_object('issued_date',p_patch->'issued_date') else '{}'::jsonb end ||
      case when p_patch ? 'effective_date' then jsonb_build_object('effective_date',p_patch->'effective_date') else '{}'::jsonb end ||
      case when p_patch ? 'issuing_authority' then jsonb_build_object('authority',p_patch->'issuing_authority') else '{}'::jsonb end
    where document_id=d.id;
  end if;
  return c;
end; $$;

-- Check corrections under the same document lock used to open the revision.
-- A correction made during extraction must not be overwritten by a stale build.
create or replace function begin_categorized_revision(
  p_document jsonb, p_token uuid, p_expected_categorization jsonb
) returns jsonb language plpgsql security invoker set search_path=public,extensions as $$
declare current_categorization jsonb;
begin
  perform assert_operation('documents:ingest',p_token);
  insert into documents(source,kind,title,metadata)
  values(p_document->>'source',p_document->>'kind',p_document->>'title','{"ingest_status":"indexing"}'::jsonb)
  on conflict(source) do nothing;
  select coalesce(metadata->'categorization','{}'::jsonb) into current_categorization
    from documents where source=p_document->>'source' for update;
  if current_categorization is distinct from p_expected_categorization then
    raise exception 'Document categorization changed during extraction; retry ingestion' using errcode='40001';
  end if;
  return begin_consistent_revision(p_document,p_token);
end; $$;

-- Publication writes the complete categorization snapshot atomically with chunks.
-- Only explicitly configured registry writes create drafts; reviewed profiles are
-- never overwritten or automatically approved by upload/ingestion.
create or replace function sync_document_categorization_profile() returns trigger
language plpgsql security invoker set search_path=public as $$
declare c jsonb; v jsonb; ns text; kind text; p jsonb;
begin
  c := new.metadata->'categorization';
  ns := c->>'registry_namespace';
  if ns is null or (tg_op='UPDATE' and c is not distinct from old.metadata->'categorization') then return null; end if;
  v := c->'values';
  kind := case v->>'document_type'
    when 'act' then 'act' when 'amendment' then 'amendment' when 'rules' then 'rules'
    when 'consolidated_guidelines' then 'handbook' when 'handbook' then 'handbook'
    when 'guidelines' then 'guideline' when 'circular' then 'instruction'
    when 'clarification' then 'clarification' when 'notification' then 'notification'
    when 'judgment_order' then 'judicial' else 'unknown' end;
  p := jsonb_build_object('instrument_type',kind,'document_category',v->'document_type',
       'issued_date',v->'issued_date','effective_date',v->'effective_date',
       'jurisdiction',v->'jurisdiction','identifiers',coalesce(nullif(v->'identifiers','null'::jsonb),'[]'::jsonb),
       'reviewed',false,'categorization',c);
  insert into legal_document_profiles(namespace,document_id,profile,reviewer_id)
    values(ns,new.id,p,c->>'updated_by')
  on conflict(namespace,document_id) do update set
    profile=legal_document_profiles.profile || p, updated_at=now()
    where coalesce(legal_document_profiles.profile->>'reviewed','false') <> 'true';
  return null;
end; $$;
drop trigger if exists document_categorization_profile on documents;
create trigger document_categorization_profile after insert or update on documents
for each row execute function sync_document_categorization_profile();

revoke all on function save_document_categorization(text,jsonb,text,text),sync_document_categorization_profile(),
  begin_categorized_revision(jsonb,uuid,jsonb),document_categorization_ready()
  from public,anon,authenticated;
grant execute on function save_document_categorization(text,jsonb,text,text),sync_document_categorization_profile(),
  begin_categorized_revision(jsonb,uuid,jsonb),document_categorization_ready() to service_role;
commit;
