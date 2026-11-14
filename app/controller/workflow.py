"""Controller unico di ingresso ai servizi esistenti.

Nessuna regola su *come* parla l'utente: solo guardrail operativi
sulla richiesta strutturata e sul contesto già verificato.
"""
from __future__ import annotations

from app.context.models import (BookingStatus, ConversationContext, ConversationStep,
    Intent, OperationType, SystemResult)
from app.flows import booking, cancel, reschedule
from app.flows.common import abandon_current_request, reject_offered_slots

_HANDLERS = {
    OperationType.CREATE: booking,
    OperationType.RESCHEDULE: reschedule,
    OperationType.CANCEL: cancel,
}


def _not_allowed(context, error='UNHANDLED_STATE'):
    return context, SystemResult(success=False, error_code=error)


def dispatch(context: ConversationContext, **kwargs):
    intent = context.conversation.current_intent
    step = context.conversation.current_step
    operation = context.operation.type
    handler = _HANDLERS.get(operation)

    if intent == Intent.ABANDON:
        if step == ConversationStep.EXECUTING:
            return _not_allowed(context)
        return abandon_current_request(context, **kwargs)
    if intent == Intent.BOOK:
        return booking.start_search(context, **kwargs)
    if intent == Intent.RESCHEDULE:
        return reschedule.identify_target(context, **kwargs)
    if intent == Intent.CANCEL:
        return cancel.identify_target(context, **kwargs)

    if intent == Intent.CHANGE_PREFERENCE and step != ConversationStep.COMPLETED:
        if operation in (OperationType.CREATE, OperationType.RESCHEDULE):
            return handler.start_search(context, **kwargs)
    if intent == Intent.SELECT_SLOT and context.offered_slots:
        if operation in (OperationType.CREATE, OperationType.RESCHEDULE):
            return handler.slot_selected(context, **kwargs)
    if intent == Intent.SELECT_APPOINTMENT and context.selected_appointment_id:
        if operation in (OperationType.RESCHEDULE, OperationType.CANCEL):
            return handler.appointment_selected(context, **kwargs)
    if intent == Intent.CONFIRM and context.confirmation.required and handler:
        return handler.confirm(context, **kwargs)
    if intent == Intent.REJECT:
        if context.confirmation.required and handler:
            return handler.reject_confirmation(context, **kwargs)
        if context.offered_slots:
            return reject_offered_slots(context, **kwargs)
    if (intent == Intent.PROVIDE_DATA
        and step == ConversationStep.COLLECTING_CUSTOMER_DATA
        and operation == OperationType.CREATE):
        return booking.customer_data_provided(context, **kwargs)

    return _not_allowed(context)
