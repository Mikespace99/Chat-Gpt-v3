from datetime import date, datetime, time, timedelta, timezone

from app.context.models import (ConversationContext, Conversation, Confirmation,
    AvailableSlot, OfferedSlot, OperationType, Intent)
from app.controller.confirmation import issue_confirmation, is_valid_confirmation, invalidate_confirmation
from app.ai.interpreter import interpret_json
from app.simulator.cli import simulate_turn


def ready_context():
    c = ConversationContext(conversation=Conversation(id='c42'))
    c.customer.id = 'customer42'
    c.customer.full_name.value = 'Mario Rossi'
    c.operation.type = OperationType.CREATE
    c.offered_slots = [OfferedSlot(option=1, slot=AvailableSlot(
        id='slot-1', date=date(2026, 11, 20), time=time(17, 30)))]
    c.booking.slot_id = 'slot-1'
    c.confirmation = Confirmation(required=True, confirmation_type='booking')
    return c


def test_confirmation_bound_to_full_proposal():
    c = ready_context()
    issue_confirmation(c)
    assert is_valid_confirmation(c)
    c.booking.slot_id = 'slot-2'
    assert not is_valid_confirmation(c)


def test_confirmation_bound_to_customer():
    c = ready_context()
    issue_confirmation(c)
    c.customer.id = 'another'
    assert not is_valid_confirmation(c)


def test_confirmation_bound_to_target_appointment():
    c = ready_context()
    c.operation.type = OperationType.RESCHEDULE
    c.operation.target_appointment_id = 'old-1'
    issue_confirmation(c)
    c.operation.target_appointment_id = 'old-2'
    assert not is_valid_confirmation(c)


def test_confirmation_expires_and_cannot_be_reused_after_invalidation():
    c = ready_context()
    now = datetime.now(timezone.utc)
    issue_confirmation(c, now=now)
    assert is_valid_confirmation(c, now=now + timedelta(minutes=14))
    assert not is_valid_confirmation(c, now=now + timedelta(minutes=16))
    invalidate_confirmation(c)
    assert not is_valid_confirmation(c)


def test_confirmation_not_allowed_when_never_issued():
    assert not is_valid_confirmation(ready_context())


def test_secondary_information_is_preserved():
    r = interpret_json({'requests': [
        {'intent': 'ASK_INFORMATION', 'entities': {}, 'topic': 'prezzo visita'},
        {'intent': 'BOOK', 'entities': {'weekday': 'venerdi'}}]}, {})
    assert r.intent == Intent.BOOK
    assert r.context_updates['secondary_questions'] == ['prezzo visita']


def test_simulator_no_side_effect():
    output = simulate_turn({'message': 'confermo', 'ai_json': {
        'requests': [{'intent': 'CONFIRM', 'entities': {}}]}})
    assert output['side_effects'] == 'NONE'
    assert output['controller_decision']['requires_clarification']


def test_simulator_rejects_unknown_json_field():
    output = simulate_turn({'message': 'x', 'ai_json': {'requests': [
        {'intent': 'BOOK', 'entities': {'sql': 'DELETE FROM customers'}}]}})
    assert output['side_effects'] == 'NONE'
    assert output['error'] == 'ValidationError'


def test_cancel_different_from_abandon():
    cancel = interpret_json({'requests': [{'intent': 'CANCEL', 'entities': {}}]}, {})
    abandon = interpret_json({'requests': [{'intent': 'ABANDON', 'entities': {}}]}, {})
    assert cancel.intent == Intent.CANCEL
    assert abandon.intent == Intent.ABANDON


def test_abandon_and_cancel_same_message_must_clarify():
    mixed = interpret_json({'requests': [
        {'intent': 'CANCEL', 'entities': {}},
        {'intent': 'ABANDON', 'entities': {}}]}, {})
    assert mixed.needs_clarification


def test_explicit_cancel_not_overridden_by_slot_matcher():
    from app.context.context_manager import apply_ai1_result
    from app.context.models import AI1Result, OfferedSlot
    c = ready_context()
    c.offered_slots = [OfferedSlot(option=1, slot=AvailableSlot(
        id='slot-1', date=date(2026, 11, 20), time=time(17, 30)))]
    r = AI1Result(intent=Intent.CANCEL, entities={'slot_number': 1}, confidence=1.0)
    updated = apply_ai1_result(c, r, 'Cancella il mio appuntamento numero 1')
    assert updated.conversation.current_intent == Intent.CANCEL
