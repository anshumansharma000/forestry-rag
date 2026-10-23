-- Preserve web provenance when chat history is reloaded. No existing rows change.
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
      'outcome',p_response->>'outcome','abstained',p_response->'abstained','retrieval_confidence',p_response->'confidence',
      'answer_origin',coalesce(p_response->>'answer_origin','documents'),
      'web_sources',coalesce(p_response->'web_sources','[]'::jsonb),
      'web_search_entry_point',p_response->'web_search_entry_point'),
    u.created_at+interval '1 microsecond') returning * into a;
  result:=p_response||jsonb_build_object('user_message',to_jsonb(u),'assistant_message',to_jsonb(a),'request_id',p_request_id);
  update chat_turns set response=result where session_id=p_session and request_id=p_request_id;
  update chat_sessions set updated_at=clock_timestamp() where id=p_session;
  return result;
end; $$;

