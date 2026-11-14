"""No live credentials: verifies retries, safe dispatch and SQL RPC contracts."""
import asyncio
import importlib.util
import sys
import types
from uuid import uuid4

# Some local test environments omit the Supabase SDK; runtime Render installs it.
if importlib.util.find_spec("supabase") is None:
    sdk = types.ModuleType("supabase")
    sdk.Client = type("Client", (), {})
    sdk.create_client = lambda *args, **kwargs: None
    sys.modules["supabase"] = sdk

import pytest

from app.operations import idempotency
from app.messaging import store, worker


class FakeRequest:
    def __init__(self, owner, table=None, action=None, payload=None):
        self.owner = owner
        self.table = table
        self.action = action
        self.payload = payload
    def upsert(self, data, **kwargs):
        self.owner.calls.append(("upsert", self.table, data, kwargs))
        return self
    def execute(self):
        return type("Result", (), {"data": self.owner.result})()


class FakeSupabase:
    def __init__(self, result=None):
        self.calls = []
        self.result = result
    def rpc(self, name, args):
        self.calls.append(("rpc", name, args))
        return FakeRequest(self, action=name)
    def table(self, name):
        return FakeRequest(self, table=name)


def test_enqueue_preserves_meta_message_id(monkeypatch):
    sb = FakeSupabase()
    monkeypatch.setattr(store, "get_supabase", lambda: sb)
    store.enqueue({"message_id": "wamid.01", "to": "+39a", "from": "123", "message": "ok"})
    kind, table, payload, kwargs = sb.calls[-1]
    assert (kind, table, payload["message_id"]) == ("upsert", "whatsapp_inbox", "wamid.01")
    assert kwargs["on_conflict"] == "message_id"
    assert kwargs["ignore_duplicates"] is True


def test_idempotency_id_comes_only_from_backend_context(monkeypatch):
    sb = FakeSupabase(result={"success": True, "appointment_id": "real-id"})
    monkeypatch.setattr(idempotency, "get_supabase", lambda: sb)
    with pytest.raises(ValueError, match="priva"):
        idempotency.execute(tenant_id=str(uuid4()), customer_id=str(uuid4()), kind="create")
    proposal = str(uuid4())
    with idempotency.operation_scope(proposal):
        result = idempotency.execute(tenant_id=str(uuid4()), customer_id=str(uuid4()),
            kind="cancel", appointment_id=str(uuid4()))
    assert result["appointment_id"] == "real-id"
    assert sb.calls[-1][1] == "agent_execute_operation"
    assert sb.calls[-1][2]["p_operation"] == proposal
    assert idempotency.active_operation_id() is None


def test_reused_operation_stable_hash(monkeypatch):
    sb = FakeSupabase(result={"success": True, "appointment_id": "a"})
    monkeypatch.setattr(idempotency, "get_supabase", lambda: sb)
    p, t, c, a = [str(uuid4()) for _ in range(4)]
    with idempotency.operation_scope(p):
        idempotency.execute(tenant_id=t, customer_id=c, kind="cancel", appointment_id=a)
        idempotency.execute(tenant_id=t, customer_id=c, kind="cancel", appointment_id=a)
    first, second = [x[2] for x in sb.calls]
    assert first["p_hash"] == second["p_hash"]
    assert len(first["p_hash"]) == 64


def test_inbox_failure_is_retried_not_finalized(monkeypatch):
    calls = []
    async def fake_claim(*args):
        return None
    monkeypatch.setattr(store, "claim_inbox", lambda w: [
        {"id": "x", "payload": {"from": "1", "to": "2", "message": "hello"}}
    ])
    def fail(*args):
        raise RuntimeError("broken")
    monkeypatch.setattr(worker, "prepare_whatsapp_message", fail)
    monkeypatch.setattr(store, "retry_inbox", lambda *a: calls.append("retry"))
    monkeypatch.setattr(store, "finish_inbox", lambda *a: calls.append("finish"))
    assert asyncio.run(worker.process_inbox_once("worker")) == 1
    assert calls == ["retry"]


def test_context_and_outbox_finalized_together(monkeypatch):
    captured = []
    monkeypatch.setattr(store, "claim_inbox", lambda w: [
        {"id": "x", "payload": {"from": "1", "to": "2", "message": "ciao"}}
    ])
    def prep(*args):
        return type("Result", (), {"conversation_id": "conv",
            "context": {"safe": True},
            "outgoing": type("Outgoing", (), {"texts": ["uno", "due"],
                "buttons": [("yes", "Sì")], "list_rows": [], "list_button_label": None})()})()
    monkeypatch.setattr(worker, "prepare_whatsapp_message", prep)
    monkeypatch.setattr(store, "finish_inbox", lambda *a: captured.append(a))
    assert asyncio.run(worker.process_inbox_once("w")) == 1
    responses = captured[0][-1]
    assert responses == [{"text": "uno"}, {"text": "due", "buttons": [["yes", "Sì"]]}]


def test_ambiguous_meta_send_not_automatically_retried(monkeypatch):
    results = []
    monkeypatch.setattr(store, "quarantine_expired_outbox", lambda: 0)
    monkeypatch.setattr(store, "claim_outbox", lambda w: [
        {"id": "out1", "payload": {"text": "Confermato"},
         "recipient_phone": "1", "tenant_phone": "2"}
    ])
    async def ambiguous(*args):
        return None
    monkeypatch.setattr(worker, "_send", ambiguous)
    monkeypatch.setattr(store, "outbox_result", lambda *a, **kw: results.append((a, kw)))
    assert asyncio.run(worker.process_outbox_once("w")) == 1
    assert results[0][0][2] == "uncertain"


def test_no_message_skips_outbox_send(monkeypatch):
    monkeypatch.setattr(store, "quarantine_expired_outbox", lambda: 0)
    monkeypatch.setattr(store, "claim_outbox", lambda w: [])
    assert asyncio.run(worker.process_outbox_once("w")) == 0


def test_migration_contains_atomicity_contract():
    from pathlib import Path
    sql = (Path(__file__).parents[1] / "migrations" / "004_reliable_messaging.sql").read_text()
    for fragment in ["on conflict(inbox_id,sequence) do nothing", "for update",
        "agent_finish_inbox", "agent_execute_operation", "pg_advisory_xact_lock",
        "agent_quarantine_outbox", "customer_not_in_tenant"]:
        assert fragment in sql.lower()
