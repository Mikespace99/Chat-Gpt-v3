"""Durable worker. One process embedded in Render; PostgreSQL coordinates claims.

Sending on an ambiguous timeout is NEVER retried automatically: it enters
`uncertain` for manual reconciliation. Meta does not expose a general
idempotency key for messages.
"""
from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from app.messaging import store
from app.webhook import prepare_whatsapp_message
from app.repositories.tenant import get_tenant_by_whatsapp_number
from app.config import Config
from app.integrations.whatsapp import (
    send_whatsapp_buttons, send_whatsapp_list, send_whatsapp_message,
)

log = logging.getLogger(__name__)


def _serialize(outgoing) -> list[dict]:
    texts = outgoing.texts or []
    payloads = []
    for n, text in enumerate(texts):
        item = {"text": text}
        if n == len(texts) - 1:
            if outgoing.buttons:
                item["buttons"] = [list(b) for b in outgoing.buttons]
            if outgoing.list_rows:
                item["list_rows"] = outgoing.list_rows
                item["list_button_label"] = outgoing.list_button_label or "Scegli"
        payloads.append(item)
    return payloads


async def process_inbox_once(worker_id: str) -> int:
    rows = await asyncio.to_thread(store.claim_inbox, worker_id)
    for row in rows:
        try:
            p = row["payload"]
            # legacy handler returns a prepared response but never sends in queue mode
            prepared = await asyncio.to_thread(
                prepare_whatsapp_message, p["from"], p["to"], p["message"],
                p.get("button_reply_id")
            )
            await asyncio.to_thread(
                store.finish_inbox, row["id"], worker_id,
                prepared.conversation_id, prepared.context, _serialize(prepared.outgoing)
            )
        except Exception as exc:
            log.exception("Inbox message failed (id=%s)", row.get("id"))
            try:
                await asyncio.to_thread(store.retry_inbox, row["id"], worker_id, exc)
            except Exception:
                log.exception("Could not record inbox failure")
    return len(rows)


async def _send(payload: dict, recipient: str, tenant_phone: str) -> dict | None:
    tenant = await asyncio.to_thread(get_tenant_by_whatsapp_number, tenant_phone)
    if not tenant:
        raise ValueError("Nessun tenant per il numero WhatsApp")
    info = tenant.get("info") or {}
    token = info.get("access_token") or Config.WHATSAPP_TOKEN
    phone_id = info.get("phone_number_id") or Config.WHATSAPP_PHONE_NUMBER_ID
    if payload.get("list_rows"):
        return await send_whatsapp_list(recipient, payload["text"],
            payload.get("list_button_label") or "Scegli", payload["list_rows"], token, phone_id)
    if payload.get("buttons"):
        return await send_whatsapp_buttons(recipient, payload["text"],
            [tuple(x) for x in payload["buttons"]], token, phone_id)
    return await send_whatsapp_message(recipient, payload["text"], token, phone_id)


async def process_outbox_once(worker_id: str) -> int:
    await asyncio.to_thread(store.quarantine_expired_outbox)
    rows = await asyncio.to_thread(store.claim_outbox, worker_id)
    for row in rows:
        try:
            response = await _send(row["payload"], row["recipient_phone"], row["tenant_phone"])
            if not response or not response.get("messages"):
                # Existing WhatsApp client masks timeouts and 4xx into None.
                # We cannot prove that the message was not sent.
                await asyncio.to_thread(store.outbox_result, row["id"], worker_id,
                                        "uncertain", error="ambiguous_send_result")
            else:
                message_ids = [x.get("id") for x in response.get("messages", []) if x.get("id")]
                await asyncio.to_thread(store.outbox_result, row["id"], worker_id,
                    "sent", provider_id=message_ids[0] if message_ids else None)
        except Exception as exc:
            log.exception("Outbox send uncertain (id=%s)", row.get("id"))
            try:
                await asyncio.to_thread(store.outbox_result, row["id"], worker_id,
                                         "uncertain", error=type(exc).__name__)
            except Exception:
                log.exception("Could not record outbox failure")
    return len(rows)


async def worker_loop(stop: asyncio.Event):
    worker_id = str(uuid4())
    while not stop.is_set():
        try:
            done = await process_inbox_once(worker_id)
            sent = await process_outbox_once(worker_id)
            if not done and not sent:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            log.exception("Worker iteration failed; retrying")
            try:
                await asyncio.wait_for(stop.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
