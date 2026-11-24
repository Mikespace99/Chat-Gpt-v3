from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from openai import OpenAI

from app.config import Config
from app.repositories.appointment import (
    cancel_owned_future_appointment,
    create_appointment,
    is_overlap_error,
    list_busy_for_availability,
    list_upcoming_for_customer,
    reschedule_owned_appointment,
)
from app.supabase_client import get_supabase

client = OpenAI(api_key=Config.OPENAI_API_KEY)


# ==============================================================================
# 1. STATO CONVERSAZIONE MINIMALE (Compatibile con webhook.py e worker.py)
# ==============================================================================
def new_state(existing_context: dict | None = None) -> dict:
    if not existing_context or not isinstance(existing_context, dict):
        return {"messages":}
    return {"messages": existing_context.get("messages",)[-12:]}


# ==============================================================================
# 2. CALENDARIO DINAMICO E CALCOLO SLOT LIBERI IN PYTHON
# ==============================================================================
def _build_14_days_calendar(tz_name: str) -> str:
    tz = ZoneInfo(tz_name or "Europe/Rome")
    now = datetime.now(tz)
    giorni = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
    lines = [
        f"OGGI È: {giorni[now.weekday()]} {now.strftime('%Y-%m-%d')} alle ore {now.strftime('%H:%M')}",
        "CALENDARIO PROSSIMI 14 GIORNI (usa esclusivamente queste date YYYY-MM-DD):",
    ]
    for i in range(14):
        d = now + timedelta(days=i)
        lines.append(f"- {giorni[d.weekday()]}: {d.strftime('%Y-%m-%d')}")
    return "\n".join(lines)


def _compute_free_slots_single_day(
    tenant_id: str,
    target_date: str,
    duration_min: int,
    tz_name: str,
    min_lead_hours: int = 2,
) -> list[str]:
    sb = get_supabase()
    d_obj = date.fromisoformat(target_date)
    dow = d_obj.isoweekday()  # 1 = Lunedì ... 7 = Domenica

    # 1. Festività nazionali
    hol = (
        sb.table("holidays")
        .select("id")
        .eq("date", target_date)
        .limit(1)
        .execute()
        .data or
    )
    if hol:
        return

    # 2. Chiusure straordinarie del tenant
    exc = (
        sb.table("calendar_exceptions")
        .select("type, start_time, end_time")
        .eq("tenant_id", tenant_id)
        .eq("date", target_date)
        .eq("active", True)
        .execute()
        .data or
    )
    if any(e["type"] == "closed" for e in exc):
        return

    # 3. Orari di apertura del giorno
    wh = (
        sb.table("working_hours")
        .select("start_time, end_time")
        .eq("tenant_id", tenant_id)
        .eq("day_of_week", dow)
        .eq("active", True)
        .execute()
        .data or
    )
    if not wh:
        return

    # 4. Intervalli già occupati (appuntamenti confermati + pause straordinarie)
    busy_rows = list_busy_for_availability(tenant_id, target_date, target_date)
    busy_intervals: list[tuple[datetime, datetime]] =
    for b in busy_rows:
        st = datetime.combine(d_obj, time.fromisoformat(str(b["appointment_time"])[:8]))
        en = st + timedelta(minutes=b.get("duration_minutes") or 30)
        busy_intervals.append((st, en))

    for e in exc:
        if e["type"] == "closed_period" and e.get("start_time") and e.get("end_time"):
            st = datetime.combine(d_obj, time.fromisoformat(str(e["start_time"])[:8]))
            en = datetime.combine(d_obj, time.fromisoformat(str(e["end_time"])[:8]))
            busy_intervals.append((st, en))

    # 5. Calcolo slot liberi ogni 30 minuti rispettando il preavviso minimo
    tz = ZoneInfo(tz_name or "Europe/Rome")
    earliest_allowed = (datetime.now(tz) + timedelta(hours=min_lead_hours)).replace(tzinfo=None)
    free_slots: set[str] = set()

    for w in wh:
        curr = datetime.combine(d_obj, time.fromisoformat(str(w["start_time"])[:8]))
        end_w = datetime.combine(d_obj, time.fromisoformat(str(w["end_time"])[:8]))
        while curr + timedelta(minutes=duration_min) <= end_w:
            slot_end = curr + timedelta(minutes=duration_min)
            if curr >= earliest_allowed:
                overlap = any(curr < b_end and slot_end > b_start for b_start, b_end in busy_intervals)
                if not overlap:
                    free_slots.add(curr.strftime("%H:%M"))
            curr += timedelta(minutes=30)

    return sorted(free_slots)


def _compute_free_slots_range(
    tenant_id: str,
    date_from: str,
    date_to: str,
    duration_min: int,
    tz_name: str,
    min_lead_hours: int = 2,
) -> dict[str, list[str]]:
    start_d = date.fromisoformat(date_from)
    end_d = min(date.fromisoformat(date_to), start_d + timedelta(days=6))  # Max 7 giorni per chiamata
    result: dict[str, list[str]] = {}
    curr_d = start_d
    while curr_d <= end_d:
        iso_d = curr_d.isoformat()
        slots = _compute_free_slots_single_day(tenant_id, iso_d, duration_min, tz_name, min_lead_hours)
        if slots:
            result[iso_d] = slots
        curr_d += timedelta(days=1)
    return result


# ==============================================================================
# 3. TOOL SQL DISPONIBILI PER L'AI
# ==============================================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_my_appointments",
            "description": "Recupera gli appuntamenti futuri confermati di questo cliente.",
            "parameters": {"type": "object", "properties": {}, "required":},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_free_slots",
            "description": "Restituisce gli orari liberi reali per un giorno singolo (date_from == date_to) o per un intervallo fino a 7 giorni (es. tutta la settimana prossima).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "Data inizio YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "Data fine YYYY-MM-DD (uguale a date_from per un solo giorno)"},
                    "service_name": {"type": "string", "description": "Nome esatto del servizio scelto"},
                },
                "required": ["date_from", "date_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Salva una nuova prenotazione nel database SOLO dopo conferma esplicita del cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "time": {"type": "string", "description": "HH:MM"},
                    "service_name": {"type": "string", "description": "Nome esatto del servizio"},
                    "person_name": {"type": "string", "description": "Nome e cognome della persona per cui si prenota"},
                },
                "required": ["date", "time", "service_name", "person_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Sposta un appuntamento esistente a nuova data/ora SOLO dopo conferma esplicita del cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string", "description": "UUID dell'appuntamento da spostare"},
                    "new_date": {"type": "string", "description": "Nuova data YYYY-MM-DD"},
                    "new_time": {"type": "string", "description": "Nuovo orario HH:MM"},
                },
                "required": ["appointment_id", "new_date", "new_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": "Cancella un appuntamento esistente SOLO dopo conferma esplicita del cliente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "string", "description": "UUID dell'appuntamento da cancellare"},
                },
                "required": ["appointment_id"],
            },
        },
    },
]


# ==============================================================================
# 4. ENTRY POINT CHIAMATO DA prepare_whatsapp_message
# ==============================================================================
def respond(
    state: dict,
    message_text: str,
    button_reply_id: str | None,
    tenant: dict,
    customer: dict,
    knowledge: dict,
) -> tuple[dict, str, list | None]:
    tenant_id = tenant["id"]
    customer_id = customer["id"]
    tz_name = tenant.get("timezone") or "Europe/Rome"
    min_lead_hours = int(tenant.get("min_lead_hours") or 2)
    user_input = (button_reply_id or message_text or "").strip()

    sb = get_supabase()
    services = (
        sb.table("services")
        .select("id, name, duration_minutes, price")
        .eq("tenant_id", tenant_id)
        .eq("active", True)
        .order("sort_order")
        .execute()
        .data or
    )
    services_by_name = {s["name"].strip().lower(): s for s in services}
    default_duration = services["duration_minutes"] if services else 30

    services_text = "\n".join(
        [f"- {s['name']} ({s['duration_minutes']} min, {s.get('price') or 0}€)" for s in services]
    ) or "- Consulenza standard (30 min)"

    customer_name = (customer.get("full_name") or "").strip()
    customer_info = (
        f"Nome cliente in anagrafica: {customer_name}"
        if customer_name
        else "Nome cliente in anagrafica: SCONOSCIUTO (chiedilo prima di confermare una nuova prenotazione)."
    )

    system_prompt = f"""Sei {tenant.get('assistant_name', 'Assistente')}, l'assistente virtuale di {tenant.get('business_name')}.
Rispondi su WhatsApp in italiano, in modo breve, naturale e cortese (usa il tu, messaggi concisi).

{_build_14_days_calendar(tz_name)}

{customer_info}

SERVIZI DISPONIBILI:
{services_text}

INFO STUDIO:
{json.dumps(knowledge, ensure_ascii=False)}

REGOLE OPERATIVE OBBLIGATORIE:
1. Per spostare o cancellare un appuntamento, chiama SEMPRE prima `get_my_appointments`. Se il cliente ha più appuntamenti, chiedi quale vuole modificare.
2. Verifica sempre la disponibilità con `check_free_slots` prima di proporre orari (puoi cercare anche su più giorni contemporaneamente, es. tutta la settimana prossima). Non inventare mai orari non presenti nei risultati del tool.
3. Se il cliente esprime preferenze (es. "di pomeriggio", "settimana prossima", "verso fine settimana"), filtra e proponi gli slot liberi coerenti con la sua richiesta.
4. Prima di chiamare `book_appointment`, assicurati di avere: servizio, giorno, orario e nome della persona (`person_name`).
5. Prima di chiamare `book_appointment`, `reschedule_appointment` o `cancel_appointment`, fai un breve riepilogo e chiedi conferma esplicita al cliente (es. "Confermi?").
"""

    history = list(state.get("messages",))
    history.append({"role": "user", "content": user_input})
    messages = [{"role": "system", "content": system_prompt}] + history

    for _ in range(5):
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=TOOLS,
            temperature=0.1,
        )
        msg = response.choices.message

        if not msg.tool_calls:
            reply_text = msg.content or "Come posso aiutarti?"
            history.append({"role": "assistant", "content": reply_text})
            return {"messages": history[-12:]}, reply_text, None

        messages.append(msg.model_dump(exclude_none=True))

        for tc in msg.tool_calls:
            fn_name = tc.function.name
            args = json.loads(tc.function.arguments or "{}")
            try:
                if fn_name == "get_my_appointments":
                    appts = list_upcoming_for_customer(tenant_id, customer_id, limit=5, tz_name=tz_name)
                    tool_result = {"appointments": appts}

                elif fn_name == "check_free_slots":
                    srv = services_by_name.get((args.get("service_name") or "").strip().lower())
                    dur = srv["duration_minutes"] if srv else default_duration
                    slots_map = _compute_free_slots_range(
                        tenant_id,
                        args["date_from"],
                        args["date_to"],
                        dur,
                        tz_name,
                        min_lead_hours,
                    )
                    tool_result = {"available_slots_by_date": slots_map}

                elif fn_name == "book_appointment":
                    srv_key = (args.get("service_name") or "").strip().lower()
                    srv = services_by_name.get(srv_key) or (services if services else None)
                    p_name = (args.get("person_name") or customer_name or "").strip()

                    if p_name and not customer_name:
                        sb.table("customers").update({"full_name": p_name}).eq("id", customer_id).execute()

                    res = create_appointment(
                        tenant_id=tenant_id,
                        appointment_date=args["date"],
                        appointment_time=args["time"],
                        duration_minutes=srv["duration_minutes"] if srv else default_duration,
                        source="manual",
                        customer_id=customer_id,
                        phone_number=customer.get("phone_number"),
                        service=srv["name"] if srv else args.get("service_name"),
                        service_id=srv["id"] if srv else None,
                        person_name=p_name or None,
                        notes="Prenotato via WhatsApp AI",
                    )
                    tool_result = {"status": "confirmed", "appointment": res}

                elif fn_name == "reschedule_appointment":
                    res = reschedule_owned_appointment(
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        appointment_id=args["appointment_id"],
                        appointment_date=args["new_date"],
                        appointment_time=args["new_time"],
                        timezone=tz_name,
                    )
                    tool_result = {"status": "rescheduled" if res else "not_found", "appointment": res}

                elif fn_name == "cancel_appointment":
                    res = cancel_owned_future_appointment(
                        tenant_id=tenant_id,
                        customer_id=customer_id,
                        appointment_id=args["appointment_id"],
                        tz_name=tz_name,
                    )
                    tool_result = {"status": "cancelled" if res else "not_found"}

                else:
                    tool_result = {"error": "Funzione non valida"}

            except Exception as exc:
                if is_overlap_error(exc):
                    tool_result = {"error": "Slot appena occupato da un altro cliente, scegli un altro orario."}
                else:
                    tool_result = {"error": str(exc)}

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(tool_result, ensure_ascii=False, default=str),
            })

    fallback = "Scusa, ho avuto un piccolo problema tecnico. Puoi ripetere?"
    history.append({"role": "assistant", "content": fallback})
    return {"messages": history[-12:]}, fallback, None
