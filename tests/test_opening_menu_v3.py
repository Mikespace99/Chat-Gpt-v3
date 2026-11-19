"""Regression tests for the WhatsApp welcome-menu loop.
No Meta/AI/Supabase calls are made in these tests.
"""
from app.controller.menu_selection import interpret_menu_selection, mark_menu_presented
from app.context.models import Conversation, ConversationContext, ConversationStatus, Intent
import sys
import types


def _webhook(monkeypatch):
    try:
        import supabase  # noqa: F401
    except ImportError:
        monkeypatch.setitem(sys.modules, 'supabase', types.SimpleNamespace(
            create_client=lambda *args, **kwargs: None, Client=object))
    from app import webhook
    return webhook


def test_opening_menu_buttons_are_structured_commands():
    assert interpret_menu_selection('book').intent == Intent.BOOK
    assert interpret_menu_selection('reschedule').intent == Intent.RESCHEDULE
    assert interpret_menu_selection('cancel').intent == Intent.CANCEL


def test_unknown_button_cannot_trigger_workflow_or_confirmation():
    for value in [None, '', 'other', 'confirm:abc', 'reject:abc', 'BOOK', 'book;cancel']:
        assert interpret_menu_selection(value) is None


def test_displaying_menu_changes_state_and_records_message():
    ctx = ConversationContext(conversation=Conversation(id='abc'))
    assert ctx.conversation.status == ConversationStatus.NEW
    mark_menu_presented(ctx, 'Salve')
    assert ctx.conversation.status == ConversationStatus.ACTIVE
    assert ctx.memory.recent_messages[-1].text == 'Salve'


def test_menu_click_not_sent_to_ai_and_reaches_workflow(monkeypatch):
    webhook = _webhook(monkeypatch)
    from app.orchestrator import OutgoingMessage
    context = ConversationContext(conversation=Conversation(id='abc'))
    mark_menu_presented(context, 'Ciao')
    called = {}
    monkeypatch.setattr(webhook, 'get_tenant_by_whatsapp_number', lambda p: {'id': 'tenant-1', 'timezone': 'Europe/Rome'})
    monkeypatch.setattr(webhook, 'get_or_create_customer', lambda *a: {'id': 'customer-1'})
    monkeypatch.setattr(webhook.context_repository, 'get_or_create_context', lambda *a: (context, {'id': 'conversation-1'}, False))
    monkeypatch.setattr(webhook, 'get_tenant_knowledge', lambda *a: {})
    monkeypatch.setattr(webhook, 'run_ai1_interpreter', lambda *a: (_ for _ in ()).throw(AssertionError('AI should not interpret menu choice')))
    def fake_handle_message(**kw):
        called['precomputed'] = kw['precomputed_ai1']
        return context, OutgoingMessage(texts=['Quale giorno preferisce?'])
    monkeypatch.setattr(webhook, 'handle_message', fake_handle_message)
    result = webhook.prepare_whatsapp_message('39123', '15551426351', 'Voglio prenotare', 'book')
    assert called['precomputed'].intent == Intent.BOOK
    assert result.outgoing.texts == ['Quale giorno preferisce?']


def test_first_button_bypasses_new_menu(monkeypatch):
    webhook = _webhook(monkeypatch)
    from app.orchestrator import OutgoingMessage
    context = ConversationContext(conversation=Conversation(id='abc'))
    monkeypatch.setattr(webhook, 'get_tenant_by_whatsapp_number', lambda p: {'id': 'tenant-1', 'timezone': 'Europe/Rome'})
    monkeypatch.setattr(webhook, 'get_or_create_customer', lambda *a: {'id': 'customer-1'})
    monkeypatch.setattr(webhook.context_repository, 'get_or_create_context', lambda *a: (context, {'id': 'conversation-1'}, False))
    monkeypatch.setattr(webhook, 'get_tenant_knowledge', lambda *a: {})
    monkeypatch.setattr(webhook, 'run_ai1_interpreter', lambda *a: (_ for _ in ()).throw(AssertionError('AI should not interpret menu choice')))
    monkeypatch.setattr(webhook, 'handle_message', lambda **kw: (context, OutgoingMessage(texts=['Quale giorno?'])))
    result = webhook.prepare_whatsapp_message('39123', '15551426351', 'Voglio prenotare', 'book')
    assert 'Quale giorno?' in result.outgoing.texts
    assert not result.outgoing.buttons


def test_unclear_greeting_shows_menu_only_once(monkeypatch):
    webhook = _webhook(monkeypatch)
    from app.context.models import AI1Result
    from app.orchestrator import OutgoingMessage
    context = ConversationContext(conversation=Conversation(id='abc'))
    monkeypatch.setattr(webhook, 'get_tenant_by_whatsapp_number', lambda p: {'id': 'tenant-1', 'timezone': 'Europe/Rome'})
    monkeypatch.setattr(webhook, 'get_or_create_customer', lambda *a: {'id': 'customer-1'})
    monkeypatch.setattr(webhook.context_repository, 'get_or_create_context', lambda *a: (context, {'id': 'conversation-1'}, False))
    monkeypatch.setattr(webhook, 'get_tenant_knowledge', lambda *a: {})
    monkeypatch.setattr(webhook, 'run_ai1_interpreter', lambda *a: AI1Result(intent=Intent.GREETING, confidence=1.0))
    result = webhook.prepare_whatsapp_message('39123','15551426351','Ciao')
    assert result.outgoing.buttons == webhook._MENU_BUTTONS
    assert result.context['conversation']['status'] == 'ACTIVE'
