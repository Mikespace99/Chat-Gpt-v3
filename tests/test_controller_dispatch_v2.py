"""Test del dispatch con adapter DB finto; non c'è alcuna chiamata di rete."""
import sys
import types

from app.context.models import (Conversation, ConversationContext, Intent,
    OperationType, ConversationStep, SystemResult)


def _workflow(monkeypatch):
    try:
        import supabase  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, 'supabase', types.SimpleNamespace(
            create_client=lambda *args, **kwargs: None, Client=object))
    from app.controller import workflow
    return workflow


def test_booking_uses_existing_engine_only_via_controller(monkeypatch):
    workflow = _workflow(monkeypatch)
    ctx = ConversationContext(conversation=Conversation(id='abc', current_intent=Intent.BOOK))
    ctx.operation.type = OperationType.CREATE
    calls = []

    def fake_booking(context, **kwargs):
        calls.append(kwargs)
        return context, SystemResult(success=True)

    monkeypatch.setattr(workflow.booking, 'start_search', fake_booking)
    _, result = workflow.dispatch(ctx, tenant={'id': 'test'}, knowledge={})
    assert result.success and len(calls) == 1


def test_cancel_intent_never_treated_as_abandon(monkeypatch):
    workflow = _workflow(monkeypatch)
    ctx = ConversationContext(conversation=Conversation(
        id='abc', current_intent=Intent.CANCEL, current_step=ConversationStep.IDLE))
    ctx.operation.type = OperationType.CANCEL
    called = []
    monkeypatch.setattr(workflow.cancel, 'identify_target', lambda c, **kw: (
        called.append('cancel') or c, SystemResult(success=True)))
    monkeypatch.setattr(workflow, 'abandon_current_request',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('abandon invoked')))
    _, result = workflow.dispatch(ctx, tenant={'id': 'test'}, knowledge={})
    assert called == ['cancel'] and result.success


def test_confirm_without_pending_never_calls_domain(monkeypatch):
    workflow = _workflow(monkeypatch)
    ctx = ConversationContext(conversation=Conversation(id='abc', current_intent=Intent.CONFIRM))
    ctx.operation.type = OperationType.CREATE
    monkeypatch.setattr(workflow.booking, 'confirm',
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError('unexpected write')))
    _, result = workflow.dispatch(ctx, tenant={'id': 'test'}, knowledge={})
    assert not result.success
