-- Additive helpers: old RPCs and response signatures remain available.
begin;
create index if not exists chat_messages_recent_answer_idx
on chat_messages(session_id,created_at desc,id desc);

create or replace function document_chunk_neighbors_batch(p_anchors jsonb)
returns table(anchor_id text,id uuid,document_id uuid,source text,chunk_index integer,chunk_type text,
 section_heading text,page_start integer,page_end integer,content text,token_estimate integer,metadata jsonb)
language plpgsql stable security invoker set search_path=public as $$
begin
  if jsonb_typeof(p_anchors)<>'array' or jsonb_array_length(p_anchors)>20 then
    raise exception 'Expected at most 20 anchors';
  end if;
  return query
  select a->>'anchor_id',n.*
  from jsonb_array_elements(p_anchors) a
  cross join lateral document_chunk_neighbors((a->>'document_id')::uuid,(a->>'chunk_index')::integer,
                                             1,nullif(a->>'revision_id','')::uuid) n;
end; $$;
revoke all on function document_chunk_neighbors_batch(jsonb) from public,anon,authenticated;
grant execute on function document_chunk_neighbors_batch(jsonb) to service_role;

do $optional$
begin
 if to_regclass('public.rag_lab_chunks') is not null then
 execute $ddl$
 create or replace function rag_lab_neighbors_batch(p_revision uuid,p_anchors jsonb)
 returns table(anchor_id text,id uuid,document_id uuid,source text,chunk_index integer,chunk_type text,
 section_heading text,page_start integer,page_end integer,content text,token_estimate integer,metadata jsonb)
 language plpgsql stable security invoker set search_path=public as $fn$
 begin
   if jsonb_typeof(p_anchors)<>'array' or jsonb_array_length(p_anchors)>20 then
     raise exception 'Expected at most 20 anchors';
   end if;
   return query
   select a->>'anchor_id', c.id,c.file_id,c.source,c.chunk_index,c.chunk_type,c.section_heading,
          c.page_start,c.page_end,c.content,c.token_estimate,c.metadata
   from jsonb_array_elements(p_anchors) a join rag_lab_chunks c
     on c.file_id=(a->>'document_id')::uuid and c.revision_id=p_revision
    and c.chunk_index between greatest(0,(a->>'chunk_index')::integer-1) and (a->>'chunk_index')::integer+1
   join rag_lab_revisions r on r.id=c.revision_id and r.status in ('ready','published')
   order by a->>'anchor_id',c.chunk_index;
 end; $fn$;
 $ddl$;
 execute 'revoke all on function rag_lab_neighbors_batch(uuid,jsonb) from public,anon,authenticated';
 execute 'grant execute on function rag_lab_neighbors_batch(uuid,jsonb) to service_role';
 end if;
end; $optional$;
commit;
