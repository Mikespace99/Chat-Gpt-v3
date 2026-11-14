"""Thin Supabase API client for durable inbox, outbox, and context commit."""
from __future__ import annotations

from app.supabase_client import get_supabase


def enqueue(message: dict) -> None:
    # Duplicate Meta deliveries are expected and harmless.
    get_supabase().table("whatsapp_inbox").upsert({
        "message_id": message["message_id"], "tenant_phone": message["to"],
        "sender_phone": message["from"], "payload": message,
    }, on_conflict="message_id", ignore_duplicates=True).execute()


def claim_inbox(worker_id: str, limit: int = 4) -> list[dict]:
    return get_supabase().rpc("agent_claim_inbox", {"p_worker": worker_id, "p_limit": limit}).execute().data or []


def finish_inbox(row_id: str, worker_id: str, conversation_id: str | None,
                 context: dict | None, responses: list[dict]) -> None:
    ok = get_supabase().rpc("agent_finish_inbox", {
        "p_id": row_id, "p_worker": worker_id, "p_conversation_id": conversation_id,
        "p_context": context, "p_responses": responses,
    }).execute().data
    if ok is not True:
        raise RuntimeError("Inbox lease scaduta: commit rifiutato")


def retry_inbox(row_id: str, worker_id: str, err: Exception) -> None:
    get_supabase().rpc("agent_retry_inbox", {"p_id": row_id, "p_worker": worker_id,
                                          "p_error": type(err).__name__}).execute()


def claim_outbox(worker_id: str, limit: int = 4) -> list[dict]:
    return get_supabase().rpc("agent_claim_outbox", {"p_worker": worker_id, "p_limit": limit}).execute().data or []


def outbox_result(row_id: str, worker_id: str, status: str,
                  provider_id: str | None = None, error: str | None = None):
    ok = get_supabase().rpc("agent_outbox_result", {
        "p_id": row_id, "p_worker": worker_id, "p_state": status,
        "p_provider_id": provider_id, "p_error": error,
    }).execute().data
    if ok is not True:
        raise RuntimeError("Outbox lease scaduta: commit rifiutato")


def quarantine_expired_outbox() -> int:
    return get_supabase().rpc("agent_quarantine_outbox", {}).execute().data or 0
