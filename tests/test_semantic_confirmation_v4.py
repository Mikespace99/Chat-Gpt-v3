"""v4 semantic confirmation: semantic freedom, transactional authorization unchanged."""
from datetime import date, time, timedelta, timezone, datetime
from types import SimpleNamespace

import pytest

from app.agent.semantic_confirmation import (
    ConfirmationDecision, interpret_confirmation,
)
from app.agent.settings import AgentSettings, load_settings
from app.context.models import (
    Appointment, BookingStatus, Conversation, ConversationContext,
    ConversationStep, Intent, OperationType, AI1Result, SystemResult,
)
from app.controller.confirmation import issue_confirmation


def pending():
    ctx = ConversationContext(conversation=Conversation(id='semantic-123'))
    ctx.customer.id = 'customer-abc'
    ctx.operation.type = OperationType.RESCHEDULE
    ctx.operation.target_appointment_id = 'apt-1'
    ctx.appointments = [Appointment(id='apt-1', date=date(2026, 12, 3),
                                    time=time(17), status=BookingStatus.CONFIRMED)]
    ctx.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
    ctx.confirmation.required = True
    ctx.confirmation.confirmation_type = 'reschedule_target'
    issue_confirmation(ctx)
    return ctx


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.response))])


@pytest.mark.parametrize('phrase', [
    'Ok, procediamo', 'Perfetto, facciamolo', 'Sì, va bene', 'Confermo, proceda pure',
])
def test_model_semantic_acceptance(phrase):
    client = FakeClient('{"decision":"ACCEPT","unconditional":true}')
    result = interpret_confirmation(pending(), phrase, client=client)
    assert result.decision == ConfirmationDecision.ACCEPT
    assert result.unconditional
    assert len(client.calls) == 1
    assert client.calls[0]['response_format'] == {'type':'json_object'}
    assert client.calls[0]['messages'][1]['content'].find(phrase) > 0


@pytest.mark.parametrize('payload', [
    '{"decision":"MODIFY","unconditional":false}',
    '{"decision":"UNCERTAIN","unconditional":false}',
    '{"decision":"UNRELATED","unconditional":false}',
    '{"decision":"ACCEPT","unconditional":false}',
    '{"decision":"REJECT","unconditional":true}',
    '{"decision":"ACCEPT","unconditional":"true"}',
    '{"decision":"ACCEPT","unconditional":true,"execute":true}',
    'not json',
])
def test_model_never_promotes_invalid_or_modified_to_accept(payload):
    result = interpret_confirmation(pending(), 'sì ma alle 18', client=FakeClient(payload))
    assert not (result.decision == ConfirmationDecision.ACCEPT and result.unconditional)


def test_only_accepted_with_valid_proposal_routes_to_controller(monkeypatch):
    from app import orchestrator
    from app.agent import semantic_confirmation
    monkeypatch.setattr(semantic_confirmation, 'interpret_confirmation',
                        lambda context, text: interpret_confirmation(
                            context, text, client=FakeClient('{"decision":"ACCEPT","unconditional":true}')))
    called = []

    def fake_dispatch(context, **kwargs):
        called.append(context.conversation.current_intent)
        context.confirmation.required = False
        return context, SystemResult(success=True)

    monkeypatch.setattr(orchestrator, 'dispatch', fake_dispatch)
    monkeypatch.setattr(orchestrator, 'run_ai2_responder',
                        lambda **kwargs: SimpleNamespace(message='Fatto', response_type=None))
    ctx, outgoing = orchestrator.handle_message(
        pending(), 'Ok, procediamo', {'timezone':'Europe/Rome'}, {}, {},
        precomputed_ai1=AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0))
    assert called == [Intent.CONFIRM]
    assert not ctx.confirmation.required


@pytest.mark.parametrize('decision', ['MODIFY','UNCERTAIN','UNRELATED'])
def test_nonacceptance_never_dispatches(monkeypatch, decision):
    from app import orchestrator
    from app.agent import semantic_confirmation
    monkeypatch.setattr(semantic_confirmation, 'interpret_confirmation',
                        lambda context, text: interpret_confirmation(context, text,
                            client=FakeClient('{"decision":"%s","unconditional":false}' % decision)))
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('write')))
    original = pending()
    updated, outgoing = orchestrator.handle_message(original, 'forse', {}, {})
    assert updated.confirmation.required
    assert updated.confirmation.proposal_id == original.confirmation.proposal_id
    assert outgoing.texts


def test_rejection_is_routed_to_existing_controller(monkeypatch):
    from app import orchestrator
    from app.agent import semantic_confirmation
    monkeypatch.setattr(semantic_confirmation, 'interpret_confirmation',
                        lambda context, text: interpret_confirmation(context, text,
                            client=FakeClient('{"decision":"REJECT","unconditional":false}')))
    called = []
    monkeypatch.setattr(orchestrator, 'dispatch', lambda context, **kwargs:
                        (called.append(context.conversation.current_intent) or context,
                         SystemResult(success=False, error_code='RESCHEDULE_TARGET_REJECTED')))
    monkeypatch.setattr(orchestrator, 'run_ai2_responder',
                        lambda **kwargs: SimpleNamespace(message='Capisco', response_type=None))
    orchestrator.handle_message(pending(), 'Preferisco lasciar stare', {}, {})
    assert called == [Intent.REJECT]


def test_expired_proposal_does_not_call_semantic_model(monkeypatch):
    from app import orchestrator
    from app.agent import semantic_confirmation
    monkeypatch.setattr(semantic_confirmation, 'interpret_confirmation',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('model should not run')))
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('write')))
    ctx = pending()
    ctx.confirmation.proposal_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    updated, outgoing = orchestrator.handle_message(ctx, 'Ok procediamo', {}, {})
    assert updated.conversation.current_intent == Intent.UNKNOWN
    assert 'non è più valida' in outgoing.texts[0]


def test_unexpected_button_never_sent_to_model(monkeypatch):
    from app import orchestrator
    from app.agent import semantic_confirmation
    monkeypatch.setattr(semantic_confirmation, 'interpret_confirmation',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('model should not run')))
    monkeypatch.setattr(orchestrator, 'dispatch', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('write')))
    updated, outgoing = orchestrator.handle_message(
        pending(), 'ok procediamo', {}, {}, button_reply_id='confirm:old-id')
    assert updated.confirmation.required
    assert 'non corrisponde' in outgoing.texts[0]


def test_confirmation_settings_have_bounds():
    cfg = load_settings()
    assert cfg.confirmation_semantic_enabled
    assert cfg.confirmation_timeout_seconds <= 30
    with pytest.raises(Exception):
        AgentSettings(confirmation_timeout_seconds=100)
    with pytest.raises(Exception):
        AgentSettings(confirmation_max_output_tokens=1000)
