"""La preferenza oraria relativa e' una richiesta comprensibile, non una conferma."""
from datetime import date, time
import sys
import types
from importlib.machinery import ModuleSpec

try:
    import supabase
except ImportError:
    stub = types.ModuleType("supabase")
    stub.__spec__ = ModuleSpec("supabase", loader=None)
    stub.create_client = lambda *a, **k: None
    stub.Client = object
    sys.modules.setdefault("supabase", stub)

from app.ai.interpreter import interpret_json
from app.ai.context_summary import build_ai1_input
from app.context.context_manager import apply_ai1_result
from app.context.models import (Appointment, BookingStatus, Conversation,
    ConversationContext, ConversationStep, Intent, OperationType)
from app.flows.common import build_search_collected_data
from app.flows import reschedule


def active_reschedule():
    ctx = ConversationContext(conversation=Conversation(id='relative-test'))
    ctx.operation.type = OperationType.RESCHEDULE
    ctx.conversation.current_step = ConversationStep.SEARCH_AVAILABILITY
    ctx.operation.target_appointment_id = 'appointment-1'
    ctx.appointments = [Appointment(id='appointment-1', date=date(2026, 11, 27),
        time=time(14, 30), status=BookingStatus.CONFIRMED)]
    return ctx


def signal(direction):
    return interpret_json({
        'requests': [{'intent': 'CHANGE_PREFERENCE',
                      'entities': {'time_direction': direction, 'keep_same_date': True}}],
        'needs_clarification': False,
    }, build_ai1_input(active_reschedule()))


def test_ai_context_exposes_verified_target_reference():
    ai_context = build_ai1_input(active_reschedule())
    assert ai_context['reschedule_target'] == {'date': '2026-11-27', 'time': '14:30'}
    assert 'appointment-1' not in str(ai_context)


def test_later_anchors_on_same_day_and_filters_after_original_time():
    parsed = signal('LATER')
    assert parsed.intent == Intent.CHANGE_PREFERENCE
    assert not parsed.needs_clarification
    ctx = apply_ai1_result(active_reschedule(), parsed,
                           'Solo orario, vorrei posticiparlo se possibile')
    assert ctx.search.date_from == date(2026, 11, 27)
    assert ctx.search.date_to == date(2026, 11, 27)
    assert ctx.search.after_time == time(14, 30)
    assert ctx.search.before_time is None
    params = build_search_collected_data(ctx)['preferences']
    assert params['after_time'] == '14:30'
    assert params['before_time'] is None
    assert params['date'] == '2026-11-27'
    assert not ctx.confirmation.required


def test_earlier_is_symmetric():
    ctx = apply_ai1_result(active_reschedule(), signal('EARLIER'),
                           'Vorrei anticiparlo, stesso giorno')
    assert ctx.search.before_time == time(14, 30)
    assert ctx.search.after_time is None
    assert build_search_collected_data(ctx)['preferences']['before_time'] == '14:30'


def test_no_verified_target_does_not_invent_a_reference_time():
    ctx = active_reschedule()
    ctx.operation.target_appointment_id = None
    parsed = signal('LATER')
    updated = apply_ai1_result(ctx, parsed, 'Posticiparlo')
    assert updated.search.after_time is None
    assert updated.search.preferred_date is None


def test_current_day_and_after_time_reach_original_engine(monkeypatch):
    from app.flows import common
    captured = {}
    def fake_search_availability(*, tenant, knowledge, collected_data):
        captured.update(collected_data['preferences'])
        return {'candidate_slots': [], 'result': {}}
    monkeypatch.setattr(common.engine, 'search_availability', fake_search_availability)
    ctx = apply_ai1_result(active_reschedule(), signal('LATER'), 'posticiparlo')
    reschedule.start_search(ctx, {'id': 'tenant-1'}, {})
    assert captured['date_from'] == '2026-11-27'
    assert captured['date_to'] == '2026-11-27'
    assert captured['after_time'] == '14:30'
