-- Simple agent: keep the active conversation alive when the queued message
-- and its resulting outbox entries are transactionally committed.
-- Apply after 004_reliable_messaging.sql (safe to reapply).
create or replace function public.agent_finish_inbox(
 p_id uuid,p_worker uuid,p_conversation_id uuid,p_context jsonb,p_responses jsonb
) returns boolean language plpgsql security invoker set search_path = public as $$
declare item jsonb; pos int := 0; rec public.whatsapp_inbox%rowtype;
begin
 select * into rec from public.whatsapp_inbox where id=p_id and status='processing'
   and claimed_by=p_worker and lease_until > clock_timestamp() for update;
 if not found then return false; end if;
 if p_conversation_id is not null then
   update public.conversations set context_v2=p_context,
     last_message_at=clock_timestamp(),
     timeout_at=clock_timestamp() + interval '15 minutes'
   where id=p_conversation_id;
   if not found then raise exception 'conversation_not_found'; end if;
 end if;
 for item in select value from jsonb_array_elements(p_responses) loop
   insert into public.whatsapp_outbox(inbox_id,sequence,tenant_phone,recipient_phone,payload)
   values(rec.id,pos,rec.tenant_phone,rec.sender_phone,item)
   on conflict(inbox_id,sequence) do nothing;
   pos := pos+1;
 end loop;
 update public.whatsapp_inbox set status='done',claimed_by=null,lease_until=null,
    finished_at=clock_timestamp() where id=p_id;
 return true;
end;$$;
revoke all on function public.agent_finish_inbox(uuid,uuid,uuid,jsonb,jsonb) from public,anon,authenticated;
grant execute on function public.agent_finish_inbox(uuid,uuid,uuid,jsonb,jsonb) to service_role;
