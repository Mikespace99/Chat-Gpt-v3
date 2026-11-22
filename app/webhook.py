"""Single-agent WhatsApp entry points. Meta transport and durable worker unchanged."""
from __future__ import annotations
from dataclasses import dataclass
from app.config import Config
from app.integrations.whatsapp import send_whatsapp_message, send_whatsapp_buttons
from app.repositories.tenant import get_tenant_by_whatsapp_number, get_tenant_knowledge
from app.repositories.customer import get_or_create_customer
from app.repositories.conversation import get_or_create_conversation
from app.supabase_client import get_supabase
from app.simple_agent.service import new_state, respond


@dataclass
class OutgoingMessage:
    texts: list[str]
    buttons: list[tuple[str,str]] | None = None
    list_rows: list | None = None
    list_button_label: str | None = None


@dataclass
class PreparedReply:
    conversation_id: str
    context: dict
    outgoing: OutgoingMessage


def prepare_whatsapp_message(phone: str, business_phone: str, message_text: str,
                             button_reply_id: str | None = None) -> PreparedReply:
    """Worker persists state+outbox with one existing PostgreSQL RPC."""
    tenant = get_tenant_by_whatsapp_number(business_phone)
    if not tenant: raise ValueError("tenant_non_trovato")
    customer = get_or_create_customer(tenant["id"], phone)
    conv, expired = get_or_create_conversation(tenant["id"], customer["id"], phone)
    state = new_state(None if expired else conv.get("context_v2"))
    knowledge = get_tenant_knowledge(tenant["id"]) or {}
    state, reply, buttons = respond(state, message_text, button_reply_id,
                                   tenant, customer, knowledge)
    return PreparedReply(str(conv["id"]), state, OutgoingMessage([reply], buttons))


async def handle_whatsapp_message(phone: str, business_phone: str, combined_text: str,
                                  button_reply_id: str | None = None):
    """Compatibility for direct calls; production path uses queued worker."""
    prepared = prepare_whatsapp_message(phone,business_phone,combined_text,button_reply_id)
    tenant = get_tenant_by_whatsapp_number(business_phone)
    info = tenant.get("info") or {}
    token = info.get("access_token") or Config.WHATSAPP_TOKEN
    phone_id = info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
    # Direct path is not transactional; only use the durable inbox worker for live tests.
    get_supabase().table("conversations").update({"context_v2":prepared.context}).eq(
        "id",prepared.conversation_id).execute()
    if prepared.outgoing.buttons:
        return await send_whatsapp_buttons(phone,prepared.outgoing.texts[0],
                      prepared.outgoing.buttons,token,phone_id)
    return await send_whatsapp_message(phone,prepared.outgoing.texts[0],token,phone_id)
