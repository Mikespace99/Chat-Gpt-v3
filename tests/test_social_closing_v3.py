"""Il ringraziamento finale non deve richiedere un intent operativo."""
import sys
import types

from app.ai.interpreter import interpret_json
from app.context.models import (AI1Result, Conversation, ConversationContext,
    ConversationStep, Intent, OperationType, ResponseType, SystemResult)
from app.ai.response_type_resolver import resolve_response_type


def test_social_closing_json_without_request():
    result = interpret_json({
        'dialogue_act': 'CLOSING', 'requests': [], 'needs_clarification': False
    }, {'current_step': 'COMPLETED', 'confirmation_required': False})
    assert result.intent == Intent.THANKS
    assert not result.needs_clarification


def test_social_closing_overrides_spurious_clarification_only_for_social_request():
    result = interpret_json({
        'dialogue_act': 'CLOSING', 'requests': [{'intent': 'UNKNOWN', 'entities': {}}],
        'needs_clarification': True,
    }, {'current_step': 'COMPLETED'})
    assert result.intent == Intent.THANKS
    assert not result.needs_clarification


def test_social_signal_does_not_hide_operational_request():
    result = interpret_json({
        'dialogue_act': 'THANKS', 'requests': [
            {'intent': 'RESCHEDULE', 'entities': {'weekday': 'venerdi'}}
        ]
    }, {'current_step': 'COMPLETED'})
    assert result.intent == Intent.RESCHEDULE


def test_no_op_and_no_social_signal_is_invalid():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        interpret_json({'requests': []}, {'current_step': 'COMPLETED'})


def test_thanks_after_completed_returns_goodbye_not_booking_confirmation():
    ctx = ConversationContext(conversation=Conversation(id='test'))
    ctx.conversation.current_step = ConversationStep.COMPLETED
    ctx.operation.type = OperationType.CREATE
    assert resolve_response_type(Intent.THANKS, SystemResult(success=True), ctx) == ResponseType.GOODBYE


def test_pure_thanks_does_not_mutate_booking_context(monkeypatch):
    try:
        import supabase  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, 'supabase', types.SimpleNamespace(
            create_client=lambda *args, **kwargs: None, Client=object))
    from app import orchestrator
    from app.context.models import AI2Result
    ctx = ConversationContext(conversation=Conversation(id='test'))
    ctx.conversation.current_step = ConversationStep.COMPLETED
    ctx.operation.type = OperationType.CREATE
    ctx.booking.slot_id = 'old-slot'
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: (_ for _ in ()).throw(AssertionError('AI2 should not be invoked')))
    new_ctx, outgoing = orchestrator.handle_message(
        context=ctx, message_text='A posto. Grazie mille', tenant={'timezone':'Europe/Rome'},
        knowledge={}, knowledge_texts={},
        precomputed_ai1=interpret_json({'dialogue_act':'CLOSING','requests':[]},
                                      {'current_step':'COMPLETED'}))
    assert 'buona giornata' in outgoing.texts[0]
    assert new_ctx.booking.slot_id == 'old-slot'
    assert new_ctx.conversation.current_step == ConversationStep.COMPLETED
    assert new_ctx.operation.type == OperationType.CREATE


def test_thanks_while_confirmation_pending_does_not_confirm(monkeypatch):
    try:
        import supabase  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, 'supabase', types.SimpleNamespace(
            create_client=lambda *args, **kwargs: None, Client=object))
    from app import orchestrator
    from app.context.models import AI2Result
    ctx = ConversationContext(conversation=Conversation(id='test'))
    ctx.conversation.current_step = ConversationStep.WAITING_FOR_CONFIRMATION
    ctx.operation.type = OperationType.CREATE
    ctx.confirmation.required = True
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: (_ for _ in ()).throw(AssertionError('AI2 should not be invoked')))
    new_ctx, outgoing = orchestrator.handle_message(
        context=ctx, message_text='Grazie', tenant={'timezone':'Europe/Rome'}, knowledge={},
        knowledge_texts={}, precomputed_ai1=interpret_json({'dialogue_act':'THANKS','requests':[]},
                                                           {'confirmation_required':True}))
    assert new_ctx.confirmation.required
    assert new_ctx.conversation.current_step == ConversationStep.WAITING_FOR_CONFIRMATION
