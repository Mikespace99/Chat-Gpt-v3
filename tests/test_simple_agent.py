"""Offline guardrail tests: no real OpenAI, WhatsApp or Supabase calls."""
import sys
import types
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

# Local CI can test the pure orchestration even without installed supabase SDK.
if "app.supabase_client" not in sys.modules:
    fake = types.ModuleType("app.supabase_client")
    fake.get_supabase = lambda: None
    sys.modules["app.supabase_client"] = fake

from app.simple_agent.service import new_state, _execute_tool, respond

TENANT = {"id":"tenant1", "timezone":"Europe/Rome"}
CUSTOMER = {"id":"customer1", "phone_number":"393331234567"}
KNOWLEDGE = {"services":[{"id":"service1", "name":"Visita", "duration_minutes":30}]}
NOW = datetime(2026,11,22,20,0,tzinfo=ZoneInfo("Europe/Rome"))


def test_wont_invent_slot_for_mutation():
    state = new_state()
    with patch("app.simple_agent.service._appointments",return_value=[]):
        result = _execute_tool("propose_operation",{"operation":"create", "date":"2026-11-27",
            "time":"15:00","service":"Visita","person_name":"Ada"},state,TENANT,CUSTOMER,KNOWLEDGE,NOW)
    assert "error" in result and state["pending"] is None


def test_create_proposal_does_not_write():
    state = new_state()
    state["last_slots"] = [{"date":"2026-11-27","time":"15:00"}]
    state["last_search_service"] = "Visita"
    with patch("app.simple_agent.service._appointments",return_value=[]), patch(
            "app.simple_agent.service.appointments.create_appointment") as write:
        result = _execute_tool("propose_operation",{"operation":"create", "date":"2026-11-27",
            "time":"15:00","service":"Visita","person_name":"Ada"},state,TENANT,CUSTOMER,KNOWLEDGE,NOW)
    assert result["proposal_ready"] and state["pending"]["id"]
    write.assert_not_called()


def test_different_service_cannot_use_previous_slot():
    state = new_state()
    state["last_slots"] = [{"date":"2026-11-27","time":"15:00"}]
    state["last_search_service"] = "Visita"
    with patch("app.simple_agent.service._appointments",return_value=[]):
        result = _execute_tool("propose_operation",{"operation":"create", "date":"2026-11-27",
            "time":"15:00","service":"Consulenza","person_name":"Ada"},state,TENANT,CUSTOMER,KNOWLEDGE,NOW)
    assert "error" in result and state["pending"] is None


def test_invalid_button_never_writes():
    state = new_state()
    state["pending"] = {"id":"123", "operation":"cancel"}
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, text, buttons = respond(state,"si", "confirm:wrong",TENANT,CUSTOMER,KNOWLEDGE)
    assert "non valido" in text.lower()
    write.assert_not_called()




def _pending_state():
    state = new_state()
    state["pending"] = {"id":"123", "operation":"cancel", "appointment_id":"appt1",
        "created_at":datetime.now(ZoneInfo("Europe/Rome")).isoformat()}
    return state


def _fake_ai(decision, evidence):
    import json
    from types import SimpleNamespace
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content=json.dumps({"decision": decision, "evidence": evidence})))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: response)))


def test_textual_yes_confirms_without_button():
    state = _pending_state()
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment", return_value={"id":"appt1"}) as write:
        state, reply, buttons = respond(state,"Sì, confermo",None,TENANT,CUSTOMER,KNOWLEDGE,
                                       client=_fake_ai("confirm", "Sì, confermo"))
    assert "completata" in reply.lower()
    assert state["pending"] is None
    assert buttons is None
    write.assert_called_once()


def test_textual_refusal_never_writes():
    state = _pending_state()
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"No, annulla",None,TENANT,CUSTOMER,KNOWLEDGE,
                                       client=_fake_ai("reject", "No, annulla"))
    assert state["pending"] is None
    assert "nessuna modifica" in reply.lower()
    write.assert_not_called()


def test_ambiguous_yes_but_change_never_writes():
    state = _pending_state()
    # A changed request invalidates the old proposal, then goes to the AI agent.
    from types import SimpleNamespace
    ai = _fake_ai("change", "sì, ma")
    original = ai.chat.completions.create
    def fake_create(**kwargs):
        if kwargs.get("response_format"):
            return original(**kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="Certamente, quale orario preferisce?",tool_calls=[]))])
    ai.chat.completions.create = fake_create
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"sì, ma alle 17",None,TENANT,CUSTOMER,KNOWLEDGE,client=ai)
    assert state["pending"] is None
    assert "orario" in reply
    write.assert_not_called()


def test_unclear_reply_preserves_proposal():
    state = _pending_state()
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"forse",None,TENANT,CUSTOMER,KNOWLEDGE,
                                       client=_fake_ai("unclear", ""))
    assert state["pending"] is not None
    assert "Può rispondere" in reply
    write.assert_not_called()


def test_hallucinated_quote_cannot_confirm():
    state = _pending_state()
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"Come sarebbe?",None,TENANT,CUSTOMER,KNOWLEDGE,
                                       client=_fake_ai("confirm", "Confermo"))
    assert state["pending"] is not None
    write.assert_not_called()


def test_expired_text_confirmation_does_not_write():
    state = _pending_state()
    state["pending"]["created_at"] = "2026-01-01T00:00:00+01:00"
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"Confermo",None,TENANT,CUSTOMER,KNOWLEDGE)
    assert state["pending"] is None
    assert "scaduta" in reply
    write.assert_not_called()


def test_ai_error_is_fail_closed():
    state = _pending_state()
    from types import SimpleNamespace
    def broken(**kwargs):
        raise RuntimeError("OpenAI offline")
    ai = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=broken)))
    with patch("app.simple_agent.service.appointments.cancel_owned_future_appointment") as write:
        state, reply, buttons = respond(state,"Confermo",None,TENANT,CUSTOMER,KNOWLEDGE, client=ai)
    assert state["pending"] is not None
    write.assert_not_called()


def test_optional_buttons_are_disabled_by_default():
    state = _pending_state()
    with patch("app.simple_agent.service.SHOW_CONFIRM_BUTTONS", True):
        state, reply, buttons = respond(state,"forse",None,TENANT,CUSTOMER,KNOWLEDGE,
                                       client=_fake_ai("unclear", ""))
        assert buttons == [("confirm:123","Conferma"),("reject:123","Annulla")]
