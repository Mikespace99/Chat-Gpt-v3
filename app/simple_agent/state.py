"""Durable operational card. This is the only authority for conversation progress.

An LLM cannot write this object directly; controller applies validated patches.
Existing conversations with prior agent state start a fresh operational card while
retaining a short text history, rather than inheriting inconsistent old anchors.
"""
from __future__ import annotations
from copy import deepcopy
from pydantic import BaseModel, ConfigDict, Field


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    date_from: str | None = None
    date_to: str | None = None
    time_band: str | None = None  # morning / afternoon / evening
    exact_time: str | None = None
    selection: str = 'options'  # options / first


class Card(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: str = 'guided-v1'
    operation: str | None = None
    phase: str = 'idle'
    appointment_id: str | None = None
    original: dict | None = None
    preferences: Preferences = Field(default_factory=Preferences)
    service: str | None = None
    person_name: str | None = None
    last_search: dict | None = None
    offered_slots: list[dict] = Field(default_factory=list)
    rejected_slots: list[str] = Field(default_factory=list)
    presented_appointments: list[dict] = Field(default_factory=list)
    pending: dict | None = None
    chosen_slot: dict | None = None
    last_displayed_slots: list[dict] = Field(default_factory=list)
    history: list[dict] = Field(default_factory=list)


def new_state(raw=None) -> dict:
    if isinstance(raw, dict) and raw.get('version') == 'guided-v1':
        try:
            state = Card.model_validate(raw).model_dump()
            state['history'] = state['history'][-12:]
            state['offered_slots'] = state['offered_slots'][-100:]
            state['rejected_slots'] = state['rejected_slots'][-100:]
            return state
        except Exception:
            pass
    state = Card().model_dump()
    if isinstance(raw, dict):
        old = raw.get('history')
        if isinstance(old, list):
            state['history'] = [x for x in old if isinstance(x, dict) and x.get('role') in ('user','assistant')][-12:]
    return state


def reset_operation(state: dict, operation: str) -> None:
    """Drop old preferences, selections and confirmation, but not recent chat."""
    history = deepcopy(state.get('history', []))[-12:]
    state.clear()
    state.update(Card(operation=operation, phase='collecting', history=history).model_dump())


def remember(state: dict, message: str, reply: str) -> None:
    state['history'] = (state['history'] + [
        {'role': 'user', 'content': message[:1500]},
        {'role': 'assistant', 'content': reply[:1800]}
    ])[-12:]
