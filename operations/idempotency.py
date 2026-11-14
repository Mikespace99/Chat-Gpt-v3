"""Domain operations with durable, transactionally checked operation IDs.

Only backend workflows call this. The AI never sees or controls the operation ID.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

from app.supabase_client import get_supabase

_active_proposal: ContextVar[str | None] = ContextVar("active_proposal", default=None)


@contextmanager
def operation_scope(proposal_id: str | None):
    token = _active_proposal.set(proposal_id)
    try:
        yield
    finally:
        _active_proposal.reset(token)


def active_operation_id() -> str | None:
    return _active_proposal.get()


def execute(*, tenant_id: str, customer_id: str, kind: str, **fields) -> dict:
    operation_id = active_operation_id()
    if not operation_id:
        raise ValueError("Operazione WhatsApp priva di proposta confermata")
    UUID(str(operation_id))
    data = {"tenant_id": tenant_id, "customer_id": customer_id, "kind": kind, **fields}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    arguments = {
        "p_tenant": tenant_id, "p_operation": operation_id, "p_hash": digest,
        "p_kind": kind, "p_customer": customer_id,
        "p_appointment": fields.get("appointment_id"),
        "p_date": fields.get("appointment_date"), "p_time": fields.get("appointment_time"),
        "p_duration": fields.get("duration_minutes"), "p_phone": fields.get("phone_number"),
        "p_service": fields.get("service"), "p_service_id": fields.get("service_id"),
        "p_location_id": fields.get("location_id"), "p_person_name": fields.get("person_name"),
        "p_timezone": fields.get("timezone") or "Europe/Rome",
    }
    result = get_supabase().rpc("agent_execute_operation", arguments).execute().data
    if not isinstance(result, dict) or not result.get("success"):
        raise RuntimeError("RPC operativa senza esito confermato")
    return result
