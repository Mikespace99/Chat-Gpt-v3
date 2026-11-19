"""Regressioni: prima disponibilita' e settimane relative all'ultima offerta."""
from datetime import date, time, datetime, timezone
import sys
import types
from importlib.machinery import ModuleSpec

try:
    import supabase
except ImportError:
    stub = types.ModuleType('supabase')
    stub.__spec__ = ModuleSpec('supabase', loader=None)
    stub.create_client = lambda *args, **kwargs: None
    stub.Client = object
    sys.modules.setdefault('supabase', stub)

from app.ai.context_summary import build_ai1_input
from app.ai.interpreter import interpret_json
from app.context.context_manager import apply_ai1_result
from app.context.models import Conversation, ConversationContext, Intent, OperationType
from app.context.search_resolver import resolve_search_criteria
from app.flows import booking


def test_first_booking_search_returns_earliest_real_slot(monkeypatch):
    from app.flows import common
    seen = {}

    def fake_engine(*, tenant, knowledge, collected_data):
        seen.update(collected_data['preferences'])
        return {'candidate_slots': [
            {'date': '2026-11-23', 'time': '09:30', 'datetime': '2026-11-23T09:30:00+01:00'},
            {'date': '2026-11-23', 'time': '10:00', 'datetime': '2026-11-23T10:00:00+01:00'},
        ], 'result': {'success': True}}

    monkeypatch.setattr(common.engine, 'search_availability', fake_engine)
    ctx = ConversationContext(conversation=Conversation(id='book-test'))
    ctx.operation.type = OperationType.CREATE
    updated, result = booking.start_search(ctx, {'id': 'tenant', 'timezone': 'Europe/Rome'}, {})
    assert result.success
    assert result.data['earliest_available'] is True
    assert updated.offered_slots[0].slot.time == time(9,30)
    assert updated.search.last_offered_date_from == date(2026,11,23)
    assert seen['date_from'] is None and seen['period'] is None


def test_relative_week_moves_from_last_offer_not_current_week():
    ctx = ConversationContext(conversation=Conversation(id='week-test'))
    ctx.operation.type = OperationType.CREATE
    ctx.search.last_offered_date_from = date(2026,11,23)
    ctx.search.last_offered_date_to = date(2026,11,29)
    summary = build_ai1_input(ctx)
    assert summary['last_offered_period']['date_to'] == '2026-11-29'
    signal = interpret_json({
        'requests': [{'intent':'CHANGE_PREFERENCE','entities':{'relative_week_shift':1}}],
        'needs_clarification':False
    }, summary)
    assert signal.intent == Intent.CHANGE_PREFERENCE
    updated = apply_ai1_result(ctx, signal, 'No, la settimana successiva')
    assert (updated.search.date_from,updated.search.date_to) == (date(2026,11,30),date(2026,12,6))


def test_next_next_week_advances_again():
    ctx = ConversationContext(conversation=Conversation(id='week-test'))
    ctx.operation.type = OperationType.CREATE
    ctx.search.last_offered_date_from = date(2026,11,30)
    ctx.search.last_offered_date_to = date(2026,12,6)
    ctx.search.date_from = date(2026,11,30)
    ctx.search.date_to = date(2026,12,6)
    updated = resolve_search_criteria({'relative_week_shift':1},ctx.search,date(2026,11,19))
    assert (updated.date_from, updated.date_to) == (date(2026,12,7),date(2026,12,13))


def test_week_containing_explicit_november_30():
    ctx = ConversationContext(conversation=Conversation(id='week-test'))
    updated = resolve_search_criteria({'week_containing_date':'2026-11-30'},ctx.search,date(2026,11,19))
    assert (updated.date_from,updated.date_to) == (date(2026,11,30),date(2026,12,6))
    assert updated.preferred_date is None


def test_new_week_clears_stale_offers_and_old_constraints():
    from app.context.models import AvailableSlot, OfferedSlot
    ctx = ConversationContext(conversation=Conversation(id='week-test'))
    ctx.operation.type = OperationType.CREATE
    ctx.search.last_offered_date_to = date(2026,11,29)
    ctx.search.time_preference = 'morning'
    ctx.offered_slots = [OfferedSlot(option=1,slot=AvailableSlot(id='old',date=date(2026,11,23),time=time(9)))]
    signal = interpret_json({'requests':[{'intent':'CHANGE_PREFERENCE','entities':{'relative_week_shift':1}}]},build_ai1_input(ctx))
    updated=apply_ai1_result(ctx,signal,'No, la settimana successiva')
    assert updated.offered_slots == []
    assert updated.search.time_preference is None
