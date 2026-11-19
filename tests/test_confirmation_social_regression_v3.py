"""Regression: an isolated yes must not become a greeting during a pending target confirmation."""
from datetime import date, time
from app.context.models import (Appointment, BookingStatus, Conversation, ConversationContext,
    ConversationStep, Intent, OperationType, AI1Result, SystemResult)
from app.controller.confirmation import issue_confirmation


def _target():
    ctx = ConversationContext(conversation=Conversation(id='confirmed-target'))
    ctx.customer.id = 'customer-1'
    ctx.operation.type = OperationType.RESCHEDULE
    ctx.operation.target_appointment_id = 'apt-1'
    ctx.appointments = [Appointment(id='apt-1', date=date(2026, 11, 27),
                                    time=time(14, 30), status=BookingStatus.CONFIRMED)]
    ctx.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
    ctx.confirmation.required = True
    ctx.confirmation.confirmation_type = 'reschedule_target'
    issue_confirmation(ctx)
    return ctx


def test_yes_overrides_misclassified_social_act_and_enters_reschedule(monkeypatch):
    from app import orchestrator
    ctx = _target()
    calls = []
    def fake_dispatch(context, **kwargs):
        calls.append(context.conversation.current_intent)
        context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
        context.confirmation.required = False
        return context, SystemResult(success=True, data={'next': 'ask_preference'})
    monkeypatch.setattr(orchestrator, 'dispatch', fake_dispatch)
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: type('R', (), {'message': 'Quale orario preferisce?', 'response_type': None})())
    updated, outgoing = orchestrator.handle_message(ctx, 'Sì', {'timezone': 'Europe/Rome'}, {}, {},
        precomputed_ai1=AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0))
    assert calls == [Intent.CONFIRM]
    assert updated.conversation.current_step == ConversationStep.SEARCH_AVAILABILITY
    assert not updated.confirmation.required
    assert 'buona giornata' not in outgoing.texts[0].lower()


def test_valid_confirm_button_overrides_model_social_act(monkeypatch):
    from app import orchestrator
    ctx = _target()
    calls = []
    def fake_dispatch(context, **kw):
        calls.append(context.conversation.current_intent)
        context.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
        context.confirmation.required = False
        return context, SystemResult(success=True)
    monkeypatch.setattr(orchestrator, 'dispatch', fake_dispatch)
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: type('R', (), {'message':'Quale orario?', 'response_type': None})())
    updated, outgoing = orchestrator.handle_message(ctx, 'Sì', {'timezone':'Europe/Rome'}, {}, {},
        precomputed_ai1=AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0),
        button_reply_id=f'confirm:{ctx.confirmation.proposal_id}')
    assert calls == [Intent.CONFIRM]
    assert updated.conversation.current_step == ConversationStep.SEARCH_AVAILABILITY


def test_thanks_during_pending_confirmation_never_confirms(monkeypatch):
    from app import orchestrator
    ctx = _target()
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **k: (_ for _ in ()).throw(AssertionError('dispatch forbidden')))
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: (_ for _ in ()).throw(AssertionError('response AI forbidden')))
    updated, outgoing = orchestrator.handle_message(ctx, 'Grazie', {'timezone':'Europe/Rome'}, {}, {},
        precomputed_ai1=AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0))
    assert updated.confirmation.required
    assert 'attesa di conferma' in outgoing.texts[0]


def test_yes_without_valid_proposal_never_executes(monkeypatch):
    from app import orchestrator
    ctx = _target()
    ctx.confirmation.proposal_digest = 'invalid'
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **k: (_ for _ in ()).throw(AssertionError('dispatch forbidden')))
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: type('R', (), {'message': 'Occorre una nuova conferma', 'response_type': None})())
    updated, outgoing = orchestrator.handle_message(ctx, 'Sì', {'timezone':'Europe/Rome'}, {}, {})
    assert updated.conversation.current_intent == Intent.UNKNOWN
    assert 'buona giornata' not in outgoing.texts[0].lower()


def test_yes_with_change_is_not_automatic_confirmation():
    from app.orchestrator import _explicit_confirmation_reply
    ctx = _target()
    assert _explicit_confirmation_reply('Sì, ma alle 18', None, ctx) is None
    assert _explicit_confirmation_reply('Ok grazie', None, ctx) is None
    assert _explicit_confirmation_reply('Si', None, ctx).intent == Intent.CONFIRM


def test_stale_confirmation_button_never_becomes_goodbye(monkeypatch):
    from app import orchestrator
    ctx = _target()
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **k: (_ for _ in ()).throw(AssertionError('dispatch forbidden')))
    monkeypatch.setattr(orchestrator, 'run_ai2_responder', lambda **kw: type('R', (), {'message': 'Conferma scaduta', 'response_type': None})())
    updated, outgoing = orchestrator.handle_message(ctx, 'Sì', {'timezone':'Europe/Rome'}, {}, {},
        precomputed_ai1=AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0),
        button_reply_id='confirm:stale-proposal')
    assert updated.conversation.current_intent == Intent.UNKNOWN
    assert 'buona giornata' not in outgoing.texts[0].lower()
