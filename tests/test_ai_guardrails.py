from datetime import date, time
import pytest
from pydantic import ValidationError

from app.ai.interpreter import interpret_json, load_interpreter_instructions
from app.ai.schemas import AIEnvelope
from app.context.models import (AI1Result, AvailableSlot, Conversation,
    ConversationContext, Intent, OfferedSlot)
from app.context.context_manager import apply_ai1_result


def test_prompt_external_exists():
    assert "Non inventare" in load_interpreter_instructions()


def test_single_booking_json():
    r = interpret_json({"requests": [{"intent": "BOOK", "entities":
                       {"weekday": "venerdi", "time_preference": "afternoon"}}]}, {})
    assert r.intent == Intent.BOOK
    assert r.entities["weekday"] == "venerdi"


def test_multiple_info_and_booking_book_wins():
    r = interpret_json({"requests": [
        {"intent": "ASK_INFORMATION", "entities": {}},
        {"intent": "BOOK", "entities": {"weekday": "martedi"}}]}, {})
    assert r.intent == Intent.BOOK


def test_conflicting_operations_never_route():
    r = interpret_json({"requests": [
        {"intent": "CANCEL", "entities": {}},
        {"intent": "RESCHEDULE", "entities": {}}]}, {})
    assert r.needs_clarification and r.intent == Intent.UNKNOWN


def test_confirmation_requires_backend_context():
    r = interpret_json({"requests": [{"intent": "CONFIRM", "entities": {}}]},
                       {"confirmation_required": False})
    assert r.needs_clarification
    assert interpret_json({"requests": [{"intent": "CONFIRM", "entities": {}}]},
                          {"confirmation_required": True}).intent == Intent.CONFIRM


def test_confirm_plus_change_blocked_even_if_confirmation_pending():
    r = interpret_json({"requests": [
        {"intent": "CONFIRM", "entities": {}},
        {"intent": "CHANGE_PREFERENCE", "entities": {"exact_time": "18:00"}}]},
        {"confirmation_required": True})
    assert r.needs_clarification


@pytest.mark.parametrize("invalid", [
    {"requests": [{"intent": "DROP_TABLE", "entities": {}}]},
    {"requests": [{"intent": "BOOK", "entities": {"sql": "DELETE FROM appointments"}}]},
    {"requests": [{"intent": "BOOK", "entities": {"exact_time": "25:99"}}]},
    {"requests": [{"intent": "BOOK", "entities": {"date_from": "tomorrow"}}]},
    {"requests": [{"intent": "BOOK", "entities": {}}], "tool_calls": ["delete"]},
    {"requests": []},
])
def test_invalid_json_contract(invalid):
    with pytest.raises(ValidationError):
        AIEnvelope.model_validate(invalid)


def test_combines_preference_without_override():
    r = interpret_json({"requests": [
        {"intent": "BOOK", "entities": {"weekday": "venerdi"}},
        {"intent": "CHANGE_PREFERENCE", "entities": {"exact_time": "18:00"}}]}, {})
    assert r.entities == {"weekday": "venerdi", "exact_time": "18:00"}


def test_contradiction_requires_clarification():
    r = interpret_json({"requests": [
        {"intent": "BOOK", "entities": {"weekday": "venerdi"}},
        {"intent": "CHANGE_PREFERENCE", "entities": {"weekday": "sabato"}}]}, {})
    assert r.needs_clarification


def test_ambiguous_message_cannot_match_slot_and_modify_context():
    context = ConversationContext(conversation=Conversation(id="test"),
        offered_slots=[OfferedSlot(option=1, slot=AvailableSlot(
            id="slot1", date=date(2026, 11, 13), time=time(18, 0)))])
    signal = AI1Result(intent=Intent.UNKNOWN, entities={"slot_number": 1},
                       confidence=0, needs_clarification=True)
    out = apply_ai1_result(context, signal, "confermo ma alle 18")
    assert out.booking.slot_id is None
    assert out.conversation.current_intent == Intent.UNKNOWN
    assert context.memory.recent_messages == []
