"""Bounded LLM tool loop for availability exploration only.

A tool call is a suggestion from untrusted LLM output, *not* authorization.
Every call is validated in Python; calls with effects are deliberately absent.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.agent.settings import AgentSettings, load_settings
from app.agent.tools import TOOLS, get_business_info, search_slots
from app.ai.format_helpers import format_offered_slots, build_offered_slots_rows
from app.config import Config
from app.context.models import ConversationContext, ConversationStep, OperationType

LOG = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).with_name("prompts.md")


@dataclass
class NavigationResult:
    context: ConversationContext
    text: str
    buttons: list[tuple[str, str]] | None = None


def is_navigation_phase(context: ConversationContext, button_reply_id: str | None) -> bool:
    # All buttons, target selections and confirmations stay on the verified
    # legacy controller. Never let the agent authorize a DB mutation.
    return bool(
        not button_reply_id and not context.confirmation.required
        and context.operation.type in (OperationType.CREATE, OperationType.RESCHEDULE)
        and context.conversation.current_step in (
            ConversationStep.WAITING_FOR_SLOT,
            ConversationStep.SEARCH_AVAILABILITY,
        )
    )


def _build_messages(context: ConversationContext, text: str, tenant: dict,
                    settings: AgentSettings) -> list[dict]:
    tz_name = tenant.get("timezone") or "Europe/Rome"
    try:
        now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        now = datetime.now(ZoneInfo("Europe/Rome"))
    nav = context.agent_navigation
    target = next((a for a in context.appointments
                   if a.id == context.operation.target_appointment_id), None)
    payload = {
        "oggi": now.date().isoformat(),
        "operation": context.operation.type.value,
        "phase": context.conversation.current_step.value,
        "reschedule_target": {"date": target.date.isoformat(), "time": target.time.strftime("%H:%M")}
            if target else None,
        "offered_slots": [
            {"date": s.slot.date.isoformat(), "time": s.slot.time.strftime("%H:%M")}
            for s in context.offered_slots
        ],
        "last_search": {
            "date_from": nav.last_date_from.isoformat() if nav.last_date_from else None,
            "date_to": nav.last_date_to.isoformat() if nav.last_date_to else None,
            "after_time": nav.last_after_time.strftime("%H:%M") if nav.last_after_time else None,
            "before_time": nav.last_before_time.strftime("%H:%M") if nav.last_before_time else None,
            "band": nav.last_band,
        },
        "recent_customer_messages": [m.text[:400] for m in context.memory.recent_messages
                                     if m.role == "user"][-settings.max_recent_user_messages:] if settings.max_recent_user_messages else [],
        "current_message": text[:1200],
    }
    return [
        {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
        {"role": "system", "content": "Puoi usare solamente gli strumenti forniti. Limite di chiamate: "
         + str(settings.max_tool_calls_per_turn)
         + ". Nessuna autorizzazione a modificare database o conferme."},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def run_navigation_agent(context: ConversationContext, message_text: str,
                         tenant: dict, knowledge: dict, *,
                         settings: AgentSettings | None = None,
                         client=None) -> NavigationResult | None:
    """Returns None to delegate to the existing controller, no side effects.

    A search result only changes browsing state and selectable slots.
    No API to create/update/cancel appointments is ever exposed to the model.
    """
    cfg = settings or load_settings()
    if not cfg.enabled or not is_navigation_phase(context, None):
        return None
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=Config.OPENAI_API_KEY, timeout=cfg.request_timeout_seconds)

    history = _build_messages(context, message_text, tenant, cfg)
    current = context
    latest: dict | None = None
    info: dict | None = None
    used_calls = 0
    for _round in range(cfg.max_tool_calls_per_turn):
        if used_calls >= cfg.max_tool_calls_per_turn:
            break
        answer = client.chat.completions.create(
            model=cfg.model or Config.AI_MODEL_INTENT,
            messages=history,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0,
            max_tokens=cfg.max_output_tokens,
        )
        reply = answer.choices[0].message
        tool_calls = getattr(reply, "tool_calls", None) or []
        if not tool_calls:
            break
        history.append(reply.model_dump(exclude_none=True))
        for call in tool_calls:
            # Per-call bounded: an LLM may request multiple functions at once.
            # Calls above the limit are never executed.
            if used_calls >= cfg.max_tool_calls_per_turn:
                history.append({"role": "tool", "tool_call_id": call.id,
                                "content": '{"status":"tool_budget_exhausted"}'})
                continue
            used_calls += 1
            try:
                args = json.loads(call.function.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("Argomenti non oggetto")
                if call.function.name == "delegate_to_workflow":
                    return None
                if call.function.name == "search_slots":
                    current, result = search_slots(current, tenant, knowledge, cfg, args)
                    latest = result
                elif call.function.name == "get_business_info":
                    if args:
                        raise ValueError("Nessun argomento ammesso")
                    result = get_business_info(tenant, knowledge)
                    info = result
                else:
                    raise ValueError("Strumento non autorizzato")
            except (ValidationError, ValueError, TypeError) as exc:
                result = {"status": "invalid_tool_arguments", "reason": type(exc).__name__}
            history.append({"role": "tool", "tool_call_id": call.id,
                            "content": json.dumps(result, ensure_ascii=False, default=str)})
        # Continue to let LLM choose another read-only call if useful.

    if latest:
        if latest.get("slots"):
            text = "Ho trovato queste disponibilità verificate:\n" + format_offered_slots(current.offered_slots)
            rows = build_offered_slots_rows(current.offered_slots)
            return NavigationResult(current, text, [(row["id"], row["title"]) for row in rows])
        return NavigationResult(current, "Non risultano altri orari disponibili con questi criteri. "
                                "Vuole cambiare orario o giorno?")
    if info is not None:
        # No free-form made-up prices: render only verified backend data.
        services = info.get("services", [])
        if services:
            names = ", ".join(str(s.get("name")) for s in services if s.get("name"))
            return NavigationResult(current, f"I servizi disponibili sono: {names}.")
        return NavigationResult(current, "Per informazioni specifiche può contattare lo studio.")
    # Model didn't use a tool: let existing conversational workflow handle it.
    return None
