"""Guided WhatsApp appointment controller.

AI proposes one semantic patch. Python exclusively owns the operational card,
searches and validated commits. Public interface preserved for app.webhook:
new_state(raw), respond(state, message, button_id, tenant, customer, knowledge).
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.booking import engine
from app.operations.idempotency import operation_scope
from app.repositories import appointment as repo
from app.simple_agent.state import new_state, remember, reset_operation
from app.simple_agent.interpreter import Meaning, interpret

LOG = logging.getLogger(__name__)
BUTTONS = os.getenv('WHATSAPP_CONFIRM_BUTTONS', 'false').lower() in ('1','true','yes')
MONTHS = ('gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto','settembre','ottobre','novembre','dicembre')
DAYS = ('lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica')


def human_day(value) -> str:
    d = date.fromisoformat(str(value)[:10])
    return f'{DAYS[d.weekday()]} {d.day} {MONTHS[d.month-1]} {d.year}'


def _appointments(tenant, customer, *, day=None, page=1):
    return repo.find_upcoming_for_customer(tenant['id'], customer['id'],
        date=day, page=page, page_size=10, tz_name=tenant.get('timezone'))


def _owned(tenant, customer, appointment_id):
    if not appointment_id:
        return None
    rows = repo.find_upcoming_for_customer(tenant['id'], customer['id'],
        appointment_id=str(appointment_id), page=1, page_size=1,
        tz_name=tenant.get('timezone'))['appointments']
    return rows[0] if rows else None


def _item(row):
    return {'id': str(row['id']), 'date': str(row['appointment_date'])[:10],
        'time': str(row['appointment_time'])[:5], 'service': row.get('service'),
        'person_name': row.get('person_name')}


def _show_appointments(items, more=False):
    if not items:
        return 'Non risultano appuntamenti futuri confermati associati a questo numero.'
    lines = [f'{i}. {human_day(a["date"])} alle {a["time"]} — {a.get("service") or "Appuntamento"} ({a.get("person_name") or "senza nome"})'
             for i,a in enumerate(items,1)]
    if more:
        lines.append('Ci sono altri appuntamenti: puoi indicarmi la data per cercarli direttamente.')
    lines.append('Quale appuntamento vuoi modificare?')
    return '\n'.join(lines)


def _service(knowledge, selected=None):
    services = [s for s in (knowledge.get('services') or []) if s.get('name') and s.get('active', True)]
    if selected:
        return next((s for s in services if s['name'].casefold() == selected.casefold()), None)
    return services[0] if len(services) == 1 else None


def _time_filters(preferences):
    band = preferences.get('time_band')
    if band is None:
        return None, None
    bands = engine.TIME_BANDS
    if band == 'morning':
        return None, f"{bands['afternoon'][0]:02d}:00"
    if band == 'afternoon':
        return f"{bands['afternoon'][0]:02d}:00", f"{bands['evening'][0]:02d}:00"
    if band == 'evening':
        return f"{bands['evening'][0]:02d}:00", None
    raise ValueError('Fascia non valida')


def _search_anchor(value, state, today):
    if value == 'selected_day':
        if not state.get('original'):
            raise ValueError('Prima scegli un appuntamento')
        d = date.fromisoformat(state['original']['date'])
        return d, d
    if value == 'selected_week':
        if not state.get('original'):
            raise ValueError('Prima scegli un appuntamento')
        d = date.fromisoformat(state['original']['date'])
    elif value == 'last_search_week':
        last = state.get('last_search')
        if not last:
            raise ValueError('Manca una ricerca precedente')
        d = date.fromisoformat(last['date_from'])
    else:
        d = today
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def apply_date_patch(preferences, meaning: Meaning, state, today):
    """Atomic semantics: unmentioned fields survive, explicit changes replace."""
    if meaning.date_action == 'clear':
        preferences['date_from'] = preferences['date_to'] = None
        return
    if meaning.date_action != 'set':
        return
    if meaning.date_reference == 'explicit':
        if not meaning.date_from or not meaning.date_to:
            raise ValueError('Date esplicite incomplete')
        start, end = date.fromisoformat(meaning.date_from), date.fromisoformat(meaning.date_to)
    else:
        if not meaning.date_reference:
            raise ValueError('Riferimento temporale mancante')
        start, end = _search_anchor(meaning.date_reference, state, today)
        start += timedelta(weeks=meaning.offset_weeks, days=meaning.offset_days)
        end += timedelta(weeks=meaning.offset_weeks, days=meaning.offset_days)
    if start > end or start < today or (end-today).days > 90 or (end-start).days > 30:
        raise ValueError('Intervallo non valido o fuori orizzonte')
    preferences['date_from'], preferences['date_to'] = start.isoformat(), end.isoformat()


def _update_preferences(state, meaning, now):
    prefs = state['preferences'].copy()
    apply_date_patch(prefs, meaning, state, now.date())
    if meaning.time_action == 'clear' or (meaning.time_action == 'set' and meaning.time_band == 'any'):
        prefs['time_band'] = None
        prefs['exact_time'] = None
    elif meaning.time_action == 'set':
        if meaning.time_band:
            prefs['time_band'] = meaning.time_band
            prefs['exact_time'] = None
        if meaning.exact_time:
            from datetime import time
            prefs['exact_time'] = time.fromisoformat(meaning.exact_time).strftime('%H:%M')
            prefs['time_band'] = None
    if meaning.selection:
        prefs['selection'] = meaning.selection
    state['preferences'] = prefs


def _has_search_preference(state):
    p = state['preferences']
    return bool(p['date_from'] and p['date_to'])


def _search(state, tenant, knowledge):
    prefs = state['preferences']
    service = _service(knowledge, state.get('service'))
    if not service:
        return {'status':'needs_service'}
    state['service'] = service['name']
    after, before = _time_filters(prefs)
    excluded = state.get('rejected_slots', [])
    result = engine.search_availability(tenant, knowledge, {
        'service': service['name'],
        'preferences': {'date_from':prefs['date_from'], 'date_to':prefs['date_to'],
            'after_time':after,'before_time':before, 'excluded_slots':excluded}
    })
    if not isinstance(result, dict) or not isinstance(result.get('candidate_slots'), list):
        raise RuntimeError('Il calendario non ha restituito un risultato valido')
    slots = [{'date':str(s['date'])[:10], 'time':str(s['time'])[:5], 'service': service['name']}
             for s in result['candidate_slots']]
    if prefs.get('exact_time'):
        # Same booking engine is authoritative. Search output is truncated to 3;
        # check requested exact time directly on each requested day (max 31).
        slots = []
        first = date.fromisoformat(prefs['date_from'])
        last = date.fromisoformat(prefs['date_to'])
        for offset in range((last-first).days + 1):
            d = (first+timedelta(days=offset)).isoformat()
            target={'date':d, 'time':prefs['exact_time']}
            if engine.validate_exact_slot(tenant,knowledge,service['name'],target).get('status') == 'available':
                slots.append({**target,'service':service['name']})
    # Only actually displayed offers count as offered. Rejected vs offered are distinct.
    seen = {(s['date'],s['time']) for s in state.get('offered_slots', [])}
    slots = [s for s in slots if f"{s['date']}_{s['time']}" not in excluded and
             (s['date'],s['time']) not in seen][:3]
    for s in slots:
        if s not in state['offered_slots']:
            state['offered_slots'].append(s)
    state['offered_slots'] = state['offered_slots'][-100:]
    state['last_search'] = {'date_from':prefs['date_from'], 'date_to':prefs['date_to'],
                            'time_band':prefs['time_band'], 'exact_time':prefs['exact_time']}
    state['phase'] = 'choosing_slot'
    return {'status':'ok', 'slots':slots}


def _describe_search(state, result):
    p = state['preferences']
    start,end = p['date_from'],p['date_to']
    span = human_day(start) if start == end else f'da {human_day(start)} a {human_day(end)}'
    band = p.get('time_band')
    label = {'morning':'di mattina','afternoon':'di pomeriggio','evening':'di sera'}.get(band,'')
    suffix = f' {label}' if label else ''
    if result['status'] != 'ok':
        return 'Non riesco a verificare il calendario adesso. Nessun appuntamento è stato modificato.'
    slots = result['slots']
    if not slots:
        return f'Non ho trovato nuove disponibilità{suffix} per {span}. Vuoi cambiare giorno, fascia oraria o cercare altri orari?'
    if p.get('selection') == 'first':
        one = slots[0]
        return f'La prima disponibilità trovata{suffix} è {human_day(one["date"])} alle {one["time"]}. Vuoi scegliere questo orario?'
    return f'Disponibilità{suffix} per {span}:\n' + '\n'.join(
        f'{i}. {human_day(s["date"])} alle {s["time"]}' for i,s in enumerate(slots,1)
    ) + '\nQuale preferisci?'


def _proposal(state, tenant, customer, knowledge, now, chosen):
    operation = state['operation']
    selected = _owned(tenant, customer, state.get('appointment_id')) if operation in ('reschedule','cancel') else None
    if operation in ('reschedule','cancel') and not selected:
        return 'L’appuntamento originale non è più modificabile. Controlliamo nuovamente gli appuntamenti.'
    if operation == 'cancel':
        pending = {'id':str(uuid4()),'operation':'cancel','appointment_id':str(selected['id']),
                   'created_at':now.isoformat()}
    else:
        if not chosen:
            return 'Quale orario vuoi scegliere?'
        service = _service(knowledge, selected['service'] if selected else state.get('service'))
        if not service:
            return 'Quale servizio desideri prenotare?'
        if operation == 'create' and not state.get('person_name'):
            state['phase'] = 'awaiting_name'
            state['chosen_slot'] = chosen
            return 'A quale nome devo intestare l’appuntamento?'
        # Even if the customer names an exact slot that was never offered,
        # the booking engine (not the model) decides whether it exists.
        # A previously displayed slot is NOT a reservation and is verified again.
        verify = engine.validate_exact_slot(tenant, knowledge, service['name'], chosen)
        if verify.get('status') != 'available':
            return 'L’orario richiesto non risulta prenotabile al controllo attuale. Vuoi cercarne un altro?'
        pending = {'id':str(uuid4()),'operation':operation,
                   'appointment_id':str(selected['id']) if selected else None,
                   'date':chosen['date'],'time':chosen['time'],
                   'service':service['name'],'service_id':service.get('id'),
                   'duration_minutes':service.get('duration_minutes',30),
                   'person_name':state.get('person_name') if operation == 'create' else selected.get('person_name'),
                   'created_at':now.isoformat()}
    state['pending'] = pending
    state['phase'] = 'awaiting_confirmation'
    return _present(pending)


def _present(p):
    if p['operation']=='cancel':
        return 'Confermi la cancellazione dell’appuntamento selezionato?'
    if p['operation']=='reschedule':
        return f'Confermi lo spostamento a {human_day(p["date"])} alle {p["time"]}?'
    return f'Confermi la prenotazione di {p["service"]} per {p["person_name"]} a {human_day(p["date"])} alle {p["time"]}?'


def _buttons(state):
    p = state.get('pending')
    return [(f'confirm:{p["id"]}', 'Conferma'),(f'reject:{p["id"]}','Annulla')] if BUTTONS and p else None


def _definite_error(exc):
    details = exc.args[0] if getattr(exc,'args',None) else None
    if not isinstance(details, dict) or details.get('code') not in ('P0001','23P01','23505','23503','23514'):
        return None
    mapping = {
        'too_many_upcoming_appointments':'Questo numero ha già raggiunto il limite di appuntamenti futuri.',
        'appointment_not_found_or_not_owned':'L’appuntamento non è più modificabile.',
        'appointment_in_past':'La data scelta è ormai passata.',
        'too_many_person_names':'Il database ha ancora un limite sui nominativi: occorre aggiornare la funzione SQL.',
    }
    return mapping.get(details.get('message'), 'Il database ha rifiutato l’operazione: verifico una nuova soluzione, senza duplicare la richiesta.')


def _commit(state, tenant, customer, knowledge, now):
    p = state['pending']
    created = datetime.fromisoformat(p['created_at'])
    if created.tzinfo is None or not timedelta(0) <= now-created <= timedelta(minutes=15):
        state['pending'] = None
        return 'La proposta è scaduta. Verifichiamo una nuova disponibilità.'
    if p['operation'] in ('create','reschedule') and not p.get('commit_attempted'):
        check = engine.validate_exact_slot(tenant, knowledge, p['service'], p)
        if check.get('status') != 'available':
            state['pending'] = None
            return 'L’orario non è più disponibile. Cerchiamone un altro.'
    p['commit_attempted'] = True
    try:
        with operation_scope(p['id']):
            if p['operation']=='create':
                result = repo.create_appointment(tenant_id=tenant['id'], customer_id=customer['id'],
                    appointment_date=p['date'], appointment_time=p['time'],
                    duration_minutes=p['duration_minutes'], source='whatsapp',
                    phone_number=customer.get('phone_number'), service=p['service'],
                    service_id=p['service_id'], person_name=p['person_name'],
                    timezone=tenant.get('timezone'))
            elif p['operation']=='reschedule':
                result = repo.reschedule_owned_appointment(tenant['id'],customer['id'],p['appointment_id'],
                    appointment_date=p['date'], appointment_time=p['time'],timezone=tenant.get('timezone'))
            else:
                result = repo.cancel_owned_future_appointment(tenant['id'],customer['id'],p['appointment_id'],tenant.get('timezone'))
        if not result:
            state['pending'] = None
            return 'Non è stato possibile completare l’operazione. Ricontrolliamo l’appuntamento.'
        state['pending'] = None
        state['phase'] = 'completed'
        return 'Operazione confermata e registrata correttamente.'
    except Exception as exc:
        definitive = _definite_error(exc)
        if definitive:
            state['pending'] = None
            state['phase'] = 'collecting'
            return definitive
        LOG.exception('GUIDED_COMMIT_OUTCOME_UNCERTAIN')
        return 'Non riesco a verificare l’esito della registrazione. Non inviare una nuova richiesta: rispondi «confermo» per verificare la stessa operazione.'


def _choose_slot(state, m):
    if (m.decision == 'confirm' and state.get('phase') == 'choosing_slot'
            and state.get('preferences', {}).get('selection') == 'first'
            and state.get('last_displayed_slots')):
        first = state['last_displayed_slots'][0]
        return {'date': first['date'], 'time': first['time']}
    if m.chosen_date and m.chosen_time:
        return {'date':m.chosen_date,'time':m.chosen_time[:5]}
    if m.selector_index and state.get('phase') in ('choosing_slot','awaiting_name'):
        last = state.get('last_displayed_slots', [])
        if 1 <= m.selector_index <= len(last):
            return {'date':last[m.selector_index-1]['date'],'time':last[m.selector_index-1]['time']}
    return None


def _handle(state, m, tenant, customer, knowledge, now, appointments):
    if state.get('pending'):
        if m.decision == 'confirm':
            return _commit(state,tenant,customer,knowledge,now)
        if m.decision == 'reject':
            state['pending']=None
            state['phase']='collecting'
            return 'Nessuna modifica effettuata. Quale alternativa preferisci?'
        if m.decision not in ('change',):
            return _present(state['pending']) + ' Puoi rispondere «confermo» o indicare una modifica.'
        state['pending']=None
        state['phase']='collecting'

    if m.operation and m.operation != state.get('operation') and m.operation in ('create','reschedule','cancel'):
        reset_operation(state,m.operation)

    operation = state.get('operation')
    if m.operation == 'info':
        info = knowledge.get('services') or []
        if info:
            descriptions = ', '.join(f"{x.get('name')} ({x.get('duration_minutes')} minuti)" for x in info if x.get('name'))
            return f'I servizi disponibili sono: {descriptions}. Vuoi sapere gli orari o prenotare?'
        return 'Posso aiutarti con prenotazioni, spostamenti e cancellazioni. Che informazione cerchi?'
    if not operation:
        return 'Vuoi prenotare un appuntamento, spostarne uno oppure cancellarlo?'

    if operation in ('reschedule','cancel'):
        chosen = None
        if m.selector_id:
            chosen = _owned(tenant,customer,m.selector_id)
        elif m.selector_index and state.get('presented_appointments'):
            presented = state['presented_appointments']
            if 1 <= m.selector_index <= len(presented):
                chosen = _owned(tenant,customer,presented[m.selector_index-1]['id'])
        elif m.selector_date:
            found = _appointments(tenant,customer,day=m.selector_date)['appointments']
            if len(found) == 1:
                chosen = found[0]
            elif len(found) > 1:
                state['presented_appointments'] = [_item(x) for x in found]
                return _show_appointments(state['presented_appointments'])
        if chosen:
            ap = _item(chosen)
            if state.get('appointment_id') != ap['id']:
                state['appointment_id']=ap['id']
                state['original']=ap
                state['preferences']={'date_from':None,'date_to':None,'time_band':None,'exact_time':None,'selection':'options'}
                state['last_search']=None
                state['offered_slots']=[]
                state['rejected_slots']=[]
                state['presented_appointments']=[]
                state['last_displayed_slots']=[]
            state['phase']='collecting'
        if not state.get('appointment_id'):
            state['presented_appointments']=[_item(x) for x in appointments['appointments']]
            state['phase']='selecting_appointment'
            return _show_appointments(state['presented_appointments'],appointments.get('has_more',False))
        if operation=='cancel':
            return _proposal(state,tenant,customer,knowledge,now,None)

    if m.ambiguous:
        return (m.clarification or 'A quale data o settimana ti riferisci?')[:300]

    try:
        _update_preferences(state,m,now)
    except (ValueError,TypeError) as exc:
        LOG.warning('GUIDED_INVALID_PREFERENCE reason=%s',type(exc).__name__)
        return 'Per evitare equivoci, puoi indicarmi la data o la settimana precisa?'

    if operation == 'reschedule' and m.change_scope == 'date' and m.time_action == 'keep':
        state['preferences']['exact_time'] = state['original']['time']
        state['preferences']['time_band'] = None
    if m.person_name and operation == 'create':
        state['person_name']=m.person_name.strip()[:120]

    chosen = _choose_slot(state,m)
    if state.get('phase')=='awaiting_name' and state.get('chosen_slot') and state.get('person_name'):
        chosen=state.pop('chosen_slot')
    if chosen:
        return _proposal(state,tenant,customer,knowledge,now,chosen)

    if (operation == 'reschedule' and state.get('original') and
            not _has_search_preference(state) and
            (m.time_action == 'set' or m.change_scope == 'time')):
        # Time-only changes retain the exact date of the verified appointment.
        state['preferences']['date_from'] = state['original']['date']
        state['preferences']['date_to'] = state['original']['date']
    if not _has_search_preference(state):
        if operation == 'reschedule' and state.get('original'):
            if m.change_scope in ('date','both'):
                return 'Per quale giorno o periodo vuoi spostarlo?'
            if m.change_scope == 'time':
                state['preferences']['date_from']=state['original']['date']
                state['preferences']['date_to']=state['original']['date']
            else:
                return (f'Vuoi cambiare solo la data, solo l’orario oppure entrambi, '
                        f'per l’appuntamento di {human_day(state["original"]["date"])}?')
        else:
            return 'Per quale giorno o periodo preferisci l’appuntamento?'

    if operation=='create':
        svc=_service(knowledge,state.get('service'))
        if not svc:
            return 'Quale servizio desideri prenotare?'
        state['service']=svc['name']
    else:
        state['service']=state['original']['service']
    try:
        result=_search(state,tenant,knowledge)
    except Exception:
        LOG.exception('GUIDED_SEARCH_ERROR')
        return 'Non riesco a verificare il calendario ora. Nessuna modifica effettuata.'
    if result['status']=='needs_service':
        return 'Quale servizio desideri prenotare?'
    state['last_displayed_slots']=result['slots']
    return _describe_search(state,result)


def respond(state, message, button_id, tenant, customer, knowledge, client=None):
    """Compatible with webhook. Every external effect is controlled by Python."""
    now=datetime.now(ZoneInfo(tenant.get('timezone') or 'Europe/Rome'))
    if button_id and state.get('pending'):
        p=state['pending']
        if button_id == f'confirm:{p["id"]}':
            reply=_commit(state,tenant,customer,knowledge,now)
        elif button_id == f'reject:{p["id"]}':
            state['pending']=None
            reply='Nessuna modifica effettuata.'
        else:
            reply='Pulsante non valido o scaduto.'
        remember(state,'[pulsante WhatsApp]',reply)
        return state,reply,_buttons(state)
    try:
        # Prefetch is read-only and bounded to a single page; filtered lookups
        # for a selected ID/date are always performed by the backend.
        fetched=_appointments(tenant,customer)
        presented=[_item(r) for r in fetched['appointments']]
    except Exception:
        LOG.exception('GUIDED_APPOINTMENTS_LOOKUP_FAILED')
        reply='Non riesco a consultare gli appuntamenti ora. Riprova tra poco.'
        remember(state,message,reply)
        return state,reply,None
    try:
        m=interpret(message,state,presented,now,client=client)
        reply=_handle(state,m,tenant,customer,knowledge,now,fetched)
    except Exception:
        LOG.exception('GUIDED_INTERPRET_OR_CONTROLLER_ERROR')
        reply='Non riesco a completare questa verifica in sicurezza. Nessuna modifica è stata eseguita.'
    remember(state,message,reply)
    return state,reply,_buttons(state)
