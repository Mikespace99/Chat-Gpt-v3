-- Guided AI v3: additive migration, NEVER run 001 again.
-- Run once from Supabase SQL Editor after backup.
create table if not exists public.whatsapp_inbox (
  id uuid primary key default gen_random_uuid(),
  message_id text not null unique,
  tenant_phone text not null,
  sender_phone text not null,
  payload jsonb not null,
  status text not null default 'pending' check(status in ('pending','processing','done','dead')),
  attempts integer not null default 0,
  available_at timestamptz not null default now(),
  claimed_by uuid,
  lease_until timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  finished_at timestamptz
);
create index if not exists whatsapp_inbox_claim_idx on public.whatsapp_inbox (status,available_at,created_at);
create index if not exists whatsapp_inbox_sender_idx on public.whatsapp_inbox (tenant_phone,sender_phone,status);

create table if not exists public.whatsapp_outbox (
  id uuid primary key default gen_random_uuid(),
  inbox_id uuid references public.whatsapp_inbox(id),
  sequence integer not null,
  tenant_phone text not null,
  recipient_phone text not null,
  payload jsonb not null,
  status text not null default 'pending' check(status in ('pending','processing','sent','uncertain','dead')),
  attempts integer not null default 0,
  available_at timestamptz not null default now(),
  claimed_by uuid,
  lease_until timestamptz,
  provider_message_id text,
  last_error text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  unique(inbox_id,sequence)
);
create index if not exists whatsapp_outbox_claim_idx on public.whatsapp_outbox(status,available_at,created_at);

create table if not exists public.operation_executions (
  tenant_id uuid not null,
  operation_id uuid not null,
  request_hash text not null,
  kind text not null check(kind in ('create','reschedule','cancel')),
  appointment_id uuid,
  result jsonb not null,
  created_at timestamptz not null default now(),
  primary key(tenant_id,operation_id)
);

-- Service role access ONLY. No direct table access via browser/anon.
alter table public.whatsapp_inbox enable row level security;
alter table public.whatsapp_outbox enable row level security;
alter table public.operation_executions enable row level security;
revoke all on public.whatsapp_inbox,public.whatsapp_outbox,public.operation_executions from anon,authenticated;
grant all on public.whatsapp_inbox,public.whatsapp_outbox,public.operation_executions to service_role;

create or replace function public.agent_claim_inbox(p_worker uuid, p_limit int default 10)
returns setof public.whatsapp_inbox language plpgsql security invoker set search_path = public as $$
begin
  return query
  with picked as (
    select q.id from public.whatsapp_inbox q
    where (q.status='pending' and q.available_at <= clock_timestamp()
           or q.status='processing' and q.lease_until < clock_timestamp())
      and not exists (
        select 1 from public.whatsapp_inbox busy
        where busy.tenant_phone=q.tenant_phone and busy.sender_phone=q.sender_phone
          and busy.id <> q.id and busy.status='processing'
          and busy.lease_until >= clock_timestamp()
      )
      and not exists (
        select 1 from public.whatsapp_inbox earlier
        where earlier.tenant_phone=q.tenant_phone and earlier.sender_phone=q.sender_phone
          and earlier.id<>q.id and earlier.status in ('pending','processing')
          and (earlier.created_at,earlier.id) < (q.created_at,q.id)
      )
    order by q.created_at,q.id
    for update of q skip locked limit least(greatest(p_limit,1),25)
  )
  update public.whatsapp_inbox q set status='processing',claimed_by=p_worker,
         lease_until=clock_timestamp()+interval '5 minutes', attempts=attempts+1
  from picked where q.id=picked.id returning q.*;
end;$$;

create or replace function public.agent_finish_inbox(
 p_id uuid,p_worker uuid,p_conversation_id uuid,p_context jsonb,p_responses jsonb
) returns boolean language plpgsql security invoker set search_path = public as $$
declare item jsonb; pos int := 0; rec public.whatsapp_inbox%rowtype;
begin
 select * into rec from public.whatsapp_inbox where id=p_id and status='processing'
   and claimed_by=p_worker and lease_until > clock_timestamp() for update;
 if not found then return false; end if;
 if p_conversation_id is not null then
   update public.conversations set context_v2=p_context where id=p_conversation_id;
   if not found then raise exception 'conversation_not_found'; end if;
 end if;
 for item in select value from jsonb_array_elements(p_responses) loop
   insert into public.whatsapp_outbox(inbox_id,sequence,tenant_phone,recipient_phone,payload)
   values(rec.id,pos,rec.tenant_phone,rec.sender_phone,item) on conflict(inbox_id,sequence) do nothing;
   pos := pos+1;
 end loop;
 update public.whatsapp_inbox set status='done',claimed_by=null,lease_until=null,
    finished_at=clock_timestamp() where id=p_id;
 return true;
end;$$;

create or replace function public.agent_retry_inbox(p_id uuid,p_worker uuid,p_error text,p_max int default 8)
returns boolean language plpgsql security invoker set search_path=public as $$
begin
 update public.whatsapp_inbox set status=case when attempts >= p_max then 'dead' else 'pending' end,
   last_error=left(p_error,800),claimed_by=null,lease_until=null,
   available_at=clock_timestamp()+(least(300,power(2,least(attempts,8))::int) * interval '1 second')
 where id=p_id and claimed_by=p_worker and status='processing';
 return found;
end;$$;

create or replace function public.agent_claim_outbox(p_worker uuid,p_limit int default 10)
returns setof public.whatsapp_outbox language plpgsql security invoker set search_path=public as $$
begin
 return query with picked as (
   select q.id from public.whatsapp_outbox q
   where q.status='pending' and q.available_at<=clock_timestamp()
    and not exists (select 1 from public.whatsapp_outbox old
     where old.inbox_id=q.inbox_id and old.sequence<q.sequence and old.status <> 'sent')
   order by q.created_at,q.sequence for update of q skip locked limit least(greatest(p_limit,1),25)
 ) update public.whatsapp_outbox q set status='processing',claimed_by=p_worker,
  lease_until=clock_timestamp()+interval '90 seconds',attempts=attempts+1
 from picked where q.id=picked.id returning q.*;
end;$$;

-- A crash after an outbound HTTP attempt leaves an unknowable delivery status.
-- Quarantine expired processing leases: DO NOT automatically resend.
create or replace function public.agent_quarantine_outbox() returns integer
language plpgsql security invoker set search_path=public as $$
declare changed integer;
begin
 update public.whatsapp_outbox set status='uncertain',claimed_by=null,lease_until=null,
  last_error='worker_lease_expired_delivery_unknown'
 where status='processing' and lease_until<clock_timestamp();
 get diagnostics changed = row_count;
 return changed;
end;$$;

create or replace function public.agent_outbox_result(p_id uuid,p_worker uuid,p_state text,p_provider_id text default null,p_error text default null)
returns boolean language plpgsql security invoker set search_path=public as $$
begin
 if p_state not in ('sent','pending','uncertain','dead') then raise exception 'invalid_outbox_status'; end if;
 update public.whatsapp_outbox set status=p_state, claimed_by=null, lease_until=null,
  provider_message_id=p_provider_id, last_error=left(p_error,800),
  sent_at=case when p_state='sent' then clock_timestamp() else sent_at end,
  available_at=case when p_state='pending' then clock_timestamp()+least(300,power(2,least(attempts,8))::int)*interval '1 second' else available_at end
 where id=p_id and status='processing' and claimed_by=p_worker;
 return found;
end;$$;

-- Atomic write + idempotency. Existing DB overlap exclusion constraint remains authoritative.
create or replace function public.agent_execute_operation(
 p_tenant uuid,p_operation uuid,p_hash text,p_kind text,p_customer uuid,
 p_appointment uuid default null,p_date date default null,p_time time default null,
 p_duration integer default null,p_phone text default null,p_service text default null,
 p_service_id uuid default null,p_location_id uuid default null,p_person_name text default null,
 p_timezone text default 'Europe/Rome'
) returns jsonb language plpgsql security invoker set search_path=public as $$
declare previous public.operation_executions%rowtype; ap public.appointments%rowtype;
        result jsonb; local_now timestamp;
begin
 if p_operation is null or p_tenant is null or p_customer is null or p_hash is null or length(p_hash)<>64 then
   raise exception 'invalid_operation_identity'; end if;
 if p_kind not in ('create','reschedule','cancel') then raise exception 'invalid_operation_kind'; end if;
 if not exists (select 1 from public.customers where id=p_customer and tenant_id=p_tenant) then
   raise exception 'customer_not_in_tenant'; end if;
 -- Serialize repeated uses of the SAME operation id. Unique PK is secondary protection.
 perform pg_advisory_xact_lock(hashtextextended(p_tenant::text || '/' || p_operation::text,0));
 select * into previous from public.operation_executions
  where tenant_id=p_tenant and operation_id=p_operation;
 if found then
   if previous.request_hash<>p_hash or previous.kind<>p_kind then
     raise exception 'idempotency_key_reused_with_different_payload'; end if;
   return previous.result;
 end if;
 local_now := clock_timestamp() at time zone coalesce(nullif(p_timezone,''),'Europe/Rome');
 if p_kind='create' then
   if p_date is null or p_time is null or p_duration is null or p_duration<=0
       or p_service is null or p_person_name is null or p_phone is null then
     raise exception 'missing_booking_fields'; end if;
   if p_date+p_time <= local_now then raise exception 'appointment_in_past'; end if;
   if p_duration>1440 then raise exception 'invalid_duration'; end if;
   -- Lock per customer to make the WhatsApp 2-future-appointments policy atomic
   -- across simultaneous requests from the same number/customer.
   perform pg_advisory_xact_lock(hashtextextended('customer/'||p_tenant::text||'/'||p_customer::text,0));
   if (select count(*) from public.appointments a
       where a.tenant_id=p_tenant and a.customer_id=p_customer and a.status='confirmed'
         and (a.appointment_date+a.appointment_time)>local_now) >= 2 then
     raise exception 'too_many_upcoming_appointments'; end if;
   if p_person_name is not null and
      (select count(distinct lower(a.person_name)) from public.appointments a
       where a.tenant_id=p_tenant and a.customer_id=p_customer and a.person_name is not null
       and lower(a.person_name)<>lower(p_person_name))>=2 then
     raise exception 'too_many_person_names'; end if;
   if p_service_id is not null and not exists (
      select 1 from public.services s where s.id=p_service_id and s.tenant_id=p_tenant and s.active=true
   ) then raise exception 'inactive_or_foreign_service'; end if;
   if p_location_id is not null and not exists (
      select 1 from public.locations l where l.id=p_location_id and l.tenant_id=p_tenant and l.active=true
   ) then raise exception 'inactive_or_foreign_location'; end if;
   insert into public.appointments(tenant_id,customer_id,phone_number,service,service_id,
      location_id,appointment_date,appointment_time,duration_minutes,status,source,person_name)
   values(p_tenant,p_customer,p_phone,p_service,p_service_id,p_location_id,p_date,p_time,
      p_duration,'confirmed','whatsapp',p_person_name)
   returning * into ap;
 elsif p_kind='reschedule' then
   if p_appointment is null or p_date is null or p_time is null then raise exception 'missing_reschedule_fields'; end if;
   if p_date+p_time <= local_now then raise exception 'appointment_in_past'; end if;
   update public.appointments set appointment_date=p_date,appointment_time=p_time
    where id=p_appointment and tenant_id=p_tenant and customer_id=p_customer and status='confirmed'
      and appointment_date+appointment_time > local_now returning * into ap;
   if not found then raise exception 'appointment_not_found_or_not_owned'; end if;
 elsif p_kind='cancel' then
   if p_appointment is null then raise exception 'missing_cancel_fields'; end if;
   update public.appointments set status='cancelled'
     where id=p_appointment and tenant_id=p_tenant and customer_id=p_customer and status='confirmed'
       and appointment_date+appointment_time > local_now returning * into ap;
   if not found then raise exception 'appointment_not_found_or_not_owned'; end if;
 end if;
 result := jsonb_build_object('success',true,'appointment_id',ap.id,'operation_id',p_operation,'status',ap.status);
 insert into public.operation_executions(tenant_id,operation_id,request_hash,kind,appointment_id,result)
   values(p_tenant,p_operation,p_hash,p_kind,ap.id,result);
 return result;
end;$$;

revoke all on function public.agent_execute_operation(uuid,uuid,text,text,uuid,uuid,date,time,integer,text,text,uuid,uuid,text,text) from public,anon,authenticated;
grant execute on function public.agent_execute_operation(uuid,uuid,text,text,uuid,uuid,date,time,integer,text,text,uuid,uuid,text,text) to service_role;
revoke all on function public.agent_claim_inbox(uuid,int) from public,anon,authenticated;
revoke all on function public.agent_finish_inbox(uuid,uuid,uuid,jsonb,jsonb) from public,anon,authenticated;
revoke all on function public.agent_retry_inbox(uuid,uuid,text,int) from public,anon,authenticated;
revoke all on function public.agent_claim_outbox(uuid,int) from public,anon,authenticated;
revoke all on function public.agent_outbox_result(uuid,uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.agent_claim_inbox(uuid,int),public.agent_finish_inbox(uuid,uuid,uuid,jsonb,jsonb),
 public.agent_retry_inbox(uuid,uuid,text,int),public.agent_claim_outbox(uuid,int),
 public.agent_outbox_result(uuid,uuid,text,text,text) to service_role;

revoke all on function public.agent_quarantine_outbox() from public,anon,authenticated;
grant execute on function public.agent_quarantine_outbox() to service_role;
