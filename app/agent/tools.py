"""Read-only, typed tools. No database client is given to the model.

The old availability engine remains authoritative and checks working hours,
appointments, buffers, holidays and lead times. Only the three *displayed*
slots become selectable; any later mutation uses the old guarded workflow.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.booking import engine
from app.context.models import (
    AvailableSlot, ConversationContext, ConversationStep, OfferedSlot, PendingAction,
)
from app.agent.settings import AgentSettings
from app.utils.it_dates import today_in_tz


class SearchSlotsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_from: date | None = None
    date_to: date | None = None
    after_time: time | None = None
    before_time: time | None = None
    band: Literal["morning", "afternoon", "evening"] | None = None
    navigation: Literal["more", "new_search"] = "more"
    reject_current: bool = False
    relative_week_shift: int | None = Field(default=None, ge=-8, le=8)
    week_containing_date: date | None = None

    @model_validator(mode="after")
    def bounds(self):
        if self.date_from and self.date_to and self.date_to < self.date_from:
            raise ValueError("L'intervallo di date è invertito")
        if self.after_time and self.before_time and self.after_time >= self.before_time:
            raise ValueError("Intervallo orario vuoto")
        return self


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_slots",
            "description": "Cerca disponibilità REALI. Per 'altri orari' usa navigation=more; per nuove preferenze usa new_search, conservando la data se non cambia.",
            "parameters": SearchSlotsArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_business_info",
            "description": "Legge informazioni pubbliche dell'attività (non appuntamenti di altri clienti).",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_workflow",
            "description": "Delega al controller esistente quando il cliente sceglie uno slot, conferma, saluta, vuole cambiare operazione o fornisce dati. Non autorizza scritture.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _target_day(context: ConversationContext) -> date | None:
    if context.operation.type.value != "RESCHEDULE":
        return None
    return next((a.date for a in context.appointments
                 if a.id == context.operation.target_appointment_id), None)


def _search_dates(args: SearchSlotsArgs, context: ConversationContext,
                  tenant: dict, settings: AgentSettings) -> tuple[date, date]:
    today = today_in_tz(tenant.get("timezone"))
    nav = context.agent_navigation
    day = _target_day(context)
    # Missing date means existing search day or the verified reschedule target;
    # never silently change day on a time-only request.
    offered_days = {s.slot.date for s in context.offered_slots}
    # "Altri orari" naviga nel giorno mostrato, non nell'intera settimana
    # eventualmente usata per scoprire quel giorno.
    offered_day = next(iter(offered_days)) if len(offered_days) == 1 else None
    base = (context.search.preferred_date or offered_day or nav.last_date_from or day or today)
    if args.week_containing_date:
        monday = args.week_containing_date - timedelta(days=args.week_containing_date.weekday())
        start, end = monday, monday + timedelta(days=6)
    elif args.relative_week_shift is not None:
        monday = base - timedelta(days=base.weekday())
        start = monday + timedelta(days=7 * args.relative_week_shift)
        end = start + timedelta(days=6)
    else:
        start = args.date_from or base
        end = args.date_to or (args.date_from or
                               (nav.last_date_to if args.navigation == "more" and not offered_day
                                else base))
    if start < today:
        raise ValueError("Ricerca su date passate non consentita")
    if end < start or end > today + timedelta(days=settings.max_search_days):
        raise ValueError("Periodo oltre l'orizzonte configurato")
    return start, end


def search_slots(context: ConversationContext, tenant: dict, knowledge: dict,
                 settings: AgentSettings, arguments: dict) -> tuple[ConversationContext, dict]:
    args = SearchSlotsArgs.model_validate(arguments)
    updated = context.model_copy(deep=True)
    nav = updated.agent_navigation
    start, end = _search_dates(args, updated, tenant, settings)

    if args.reject_current:
        for offered in updated.offered_slots:
            if offered.slot.id not in nav.rejected_slot_ids:
                nav.rejected_slot_ids.append(offered.slot.id)
        nav.rejected_slot_ids = nav.rejected_slot_ids[-settings.max_remembered_slots:]

    if args.navigation == "more":
        # All slots already presented are excluded, including those offered
        # by the old workflow before the agent's first invocation.
        for offered in updated.offered_slots:
            sid = offered.slot.id
            if sid not in nav.shown_slot_ids:
                nav.shown_slot_ids.append(sid)
    else:
        # New filters reset pagination, NOT the customer's explicit rejections.
        nav.shown_slot_ids.clear()

    after = args.after_time or (nav.last_after_time if args.navigation == "more" else None)
    before = args.before_time or (nav.last_before_time if args.navigation == "more" else None)
    band = args.band or (nav.last_band if args.navigation == "more" else None)

    excluded = list(dict.fromkeys(
        nav.shown_slot_ids + nav.rejected_slot_ids + (updated.search.excluded_slots or [])
    ))
    prefs = {
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "after_time": after.strftime("%H:%M") if after else None,
        "before_time": before.strftime("%H:%M") if before else None,
        "time_preference": band,
        "excluded_slots": excluded,
    }
    raw = engine.search_availability(tenant, knowledge, {
        "service": updated.service.value,
        "location_id": updated.professional.location_id if updated.professional else None,
        "preferences": prefs,
    })
    candidates = raw.get("candidate_slots") or []
    # Defense in depth: exclude already seen slots here too, regardless
    # of how an engine implementation handles excluded_slots.
    candidates = [s for s in candidates if f"{s['date']}_{s['time']}" not in excluded]
    offered = candidates[:settings.max_slots_per_page]
    if offered:
        updated.offered_slots = [OfferedSlot(
            option=i,
            slot=AvailableSlot(id=f"{s['date']}_{s['time']}", date=s["date"], time=s["time"]),
        ) for i, s in enumerate(offered, 1)]
        updated.conversation.current_step = ConversationStep.WAITING_FOR_SLOT
        updated.conversation.pending_action = PendingAction.NONE
        for item in updated.offered_slots:
            if item.slot.id not in nav.shown_slot_ids:
                nav.shown_slot_ids.append(item.slot.id)
        nav.shown_slot_ids = nav.shown_slot_ids[-settings.max_remembered_slots:]
    # If no new slot exists, keep previously offered options usable.
    nav.last_date_from, nav.last_date_to = start, end
    nav.last_after_time, nav.last_before_time, nav.last_band = after, before, band
    updated.search.last_offered_date_from = start
    updated.search.last_offered_date_to = end
    updated.search.date_from = start
    updated.search.date_to = end
    updated.search.preferred_date = start if start == end else None
    updated.search.after_time = after
    updated.search.before_time = before
    updated.search.time_preference = band
    return updated, {
        "status": "slots_found" if offered else "no_slots_found",
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "slots": [{"date": s["date"], "time": s["time"]} for s in offered],
        "count": len(offered),
    }


def get_business_info(tenant: dict, knowledge: dict) -> dict:
    """Only low-risk tenant information, no credentials or private records."""
    return {
        "business_name": tenant.get("business_name"),
        "services": [{k: v for k, v in s.items() if k in ("name", "duration_minutes", "price", "description")}
                     for s in (knowledge.get("services") or [])[:20]],
    }
