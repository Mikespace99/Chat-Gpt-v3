from datetime import date, time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.settings import AgentSettings, load_settings
from app.agent.tools import SearchSlotsArgs, search_slots
from app.agent.runtime import is_navigation_phase, run_navigation_agent
from app.context.models import (
    Conversation, ConversationContext, ConversationStep, OperationType,
    AvailableSlot, OfferedSlot,
)


def make_context():
    ctx = ConversationContext(conversation=Conversation(id="demo"))
    ctx.operation.type = OperationType.RESCHEDULE
    ctx.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
    ctx.offered_slots = [OfferedSlot(option=i, slot=AvailableSlot(
        id=f"2026-12-03_{h}", date="2026-12-03", time=h
    )) for i, h in enumerate(["09:00", "09:30", "10:00"], 1)]
    return ctx


def fake_engine(tenant, knowledge, collected_data):
    prefs = collected_data["preferences"]
    excluded = set(prefs.get("excluded_slots") or [])
    result = []
    for hh in ("09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00"):
        if f"2026-12-03_{hh}" in excluded:
            continue
        if prefs.get("after_time") and hh <= prefs["after_time"]:
            continue
        if prefs.get("before_time") and hh >= prefs["before_time"]:
            continue
        result.append({"date": "2026-12-03", "time": hh})
    return {"candidate_slots": result[:3]}


def test_more_slots_never_repeat_previous(monkeypatch):
    monkeypatch.setattr("app.agent.tools.engine.search_availability", fake_engine)
    ctx, res = search_slots(make_context(), {"timezone": "Europe/Rome"}, {},
                            AgentSettings(), {"navigation": "more"})
    assert res["status"] == "slots_found"
    assert [s["time"] for s in res["slots"]] == ["10:30", "11:00", "11:30"]
    ctx, next_res = search_slots(ctx, {"timezone": "Europe/Rome"}, {},
                                 AgentSettings(), {"navigation": "more"})
    assert next_res["slots"] == [{"date": "2026-12-03", "time": "12:00"}]


def test_after_time_same_date_not_business_hours(monkeypatch):
    monkeypatch.setattr("app.agent.tools.engine.search_availability", fake_engine)
    ctx, res = search_slots(make_context(), {"timezone": "Europe/Rome"}, {},
                            AgentSettings(), {"navigation": "new_search", "after_time": "10:00"})
    assert res["date_from"] == "2026-12-03"
    assert all(s["time"] > "10:00" for s in res["slots"])
    assert ctx.search.after_time == time(10, 0)


def test_reject_vs_browse(monkeypatch):
    monkeypatch.setattr("app.agent.tools.engine.search_availability", fake_engine)
    ctx, res = search_slots(make_context(), {}, {}, AgentSettings(),
                            {"navigation": "more", "reject_current": True})
    assert "2026-12-03_09:00" in ctx.agent_navigation.rejected_slot_ids
    assert "2026-12-03_10:30" not in ctx.agent_navigation.rejected_slot_ids
    ctx, _ = search_slots(ctx, {}, {}, AgentSettings(),
                          {"navigation": "new_search"})
    assert "2026-12-03_09:00" in ctx.agent_navigation.rejected_slot_ids


def test_date_invalid_or_ambiguous_rejected():
    with pytest.raises(Exception):
        SearchSlotsArgs(date_from="not-a-date")
    with pytest.raises(Exception):
        SearchSlotsArgs(after_time="15:00", before_time="14:00")
    with pytest.raises(Exception):
        search_slots(make_context(), {}, {}, AgentSettings(),
                     {"date_from": "2026-12-01", "date_to": "2027-12-01"})


def test_confirmation_cannot_enter_tool_agent():
    ctx = make_context()
    ctx.confirmation.required = True
    assert not is_navigation_phase(ctx, None)
    ctx.confirmation.required = False
    assert not is_navigation_phase(ctx, "confirm:arbitrary")


def test_config_tenant_override_allowlist(tmp_path):
    from app.agent.settings import CONFIG_PATH
    cfg = load_settings(CONFIG_PATH, {"max_tool_calls_per_turn": 3, "enabled": False})
    assert cfg.max_tool_calls_per_turn == 3
    assert cfg.enabled is True
    with pytest.raises(Exception):
        load_settings(CONFIG_PATH, {"max_tool_calls_per_turn": 7})


def fake_call(name, args="{}", id="call_1"):
    return SimpleNamespace(id=id, function=SimpleNamespace(name=name, arguments=args))


class FakeMessage:
    def __init__(self, calls):
        self.tool_calls = calls
    def model_dump(self, **_):
        return {"role": "assistant", "tool_calls": [
            {"id": c.id, "type": "function", "function":
             {"name": c.function.name, "arguments": c.function.arguments}}
            for c in self.tool_calls
        ]}


class FakeClient:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
    def create(self, **kwargs):
        try:
            calls = next(self.batches)
        except StopIteration:
            calls = []
        return SimpleNamespace(choices=[SimpleNamespace(message=FakeMessage(calls))])


def test_agent_calls_tools_and_formats_only_verified_results(monkeypatch):
    monkeypatch.setattr("app.agent.tools.engine.search_availability", fake_engine)
    client = FakeClient([[fake_call("search_slots", '{"navigation":"more"}')], []])
    res = run_navigation_agent(make_context(), "Altri orari", {}, {}, client=client)
    assert res is not None
    assert "10:30" in res.text
    assert "09:00" not in res.text
    assert len(res.buttons) == 3


def test_tool_call_budget_cannot_be_bypassed_with_parallel_calls(monkeypatch):
    counter = []
    def counting(*args):
        counter.append(1)
        return fake_engine(*args)
    monkeypatch.setattr("app.agent.tools.engine.search_availability", counting)
    client = FakeClient([[fake_call("search_slots", '{"navigation":"more"}', str(i))
                          for i in range(8)]])
    result = run_navigation_agent(make_context(), "Ancora", {}, {},
                                  settings=AgentSettings(max_tool_calls_per_turn=3), client=client)
    assert len(counter) == 3
    assert result is not None


def test_unknown_tool_never_executes_and_delegates():
    client = FakeClient([[fake_call("update_appointment")], []])
    assert run_navigation_agent(make_context(), "sposta", {}, {}, client=client) is None


def test_delegate_to_workflow_leaves_context_untouched():
    ctx = make_context()
    client = FakeClient([[fake_call("delegate_to_workflow")]])
    assert run_navigation_agent(ctx, "Le 10 vanno bene", {}, {}, client=client) is None
    assert len(ctx.offered_slots) == 3


def test_browse_stays_on_offered_day_even_if_week_was_searched(monkeypatch):
    monkeypatch.setattr("app.agent.tools.engine.search_availability", fake_engine)
    ctx = make_context()
    ctx.agent_navigation.last_date_from = date(2026, 11, 30)
    ctx.agent_navigation.last_date_to = date(2026, 12, 6)
    _, result = search_slots(ctx, {}, {}, AgentSettings(), {"navigation": "more"})
    assert result["date_from"] == "2026-12-03"
    assert result["date_to"] == "2026-12-03"
