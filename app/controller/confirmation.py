"""Conferme verificabili: l'LLM non può costruire né approvare una proposta.

La proposta viene creata dal backend quando il flusso entra nella fase di
conferma; la sua impronta lega cliente, tenant, slot e appuntamento selezionato.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.context.models import ConversationContext

TTL_MINUTES = 15


def _now():
    return datetime.now(timezone.utc)


def _snapshot(ctx: ConversationContext) -> dict:
    offered = next((x.slot for x in ctx.offered_slots if x.slot.id == ctx.booking.slot_id), None)
    return {
        "conversation_id": ctx.conversation.id,
        "customer_id": ctx.customer.id,
        "customer_name": ctx.customer.full_name.value,
        "operation": ctx.operation.type.value,
        "confirmation_type": ctx.confirmation.confirmation_type,
        "appointment_id": ctx.operation.target_appointment_id,
        "slot_id": ctx.booking.slot_id,
        "slot_date": offered.date.isoformat() if offered else None,
        "slot_time": offered.time.isoformat() if offered else None,
    }


def _digest(ctx: ConversationContext) -> str:
    payload = json.dumps(_snapshot(ctx), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def issue_confirmation(ctx: ConversationContext, *, now: datetime | None = None) -> None:
    """Il controller invoca soltanto al momento di presentare la conferma."""
    if not ctx.confirmation.required or not ctx.confirmation.confirmation_type:
        return
    current = now or _now()
    if current.tzinfo is None:
        raise ValueError("Timezone richiesta")
    ctx.confirmation.proposal_id = str(uuid4())
    ctx.confirmation.proposal_digest = _digest(ctx)
    ctx.confirmation.proposal_expires_at = current + timedelta(minutes=TTL_MINUTES)
    ctx.confirmation.status = None


def is_valid_confirmation(ctx: ConversationContext, *, now: datetime | None = None) -> bool:
    c = ctx.confirmation
    current = now or _now()
    return bool(
        c.required
        and c.proposal_id
        and c.proposal_digest
        and c.proposal_digest == _digest(ctx)
        and c.proposal_expires_at
        and current < c.proposal_expires_at
        and c.status != "confirmed"
    )


def invalidate_confirmation(ctx: ConversationContext) -> None:
    ctx.confirmation.proposal_id = None
    ctx.confirmation.proposal_digest = None
    ctx.confirmation.proposal_expires_at = None
