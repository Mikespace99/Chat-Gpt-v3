"""Cancellazione sicura: scelta appuntamento, conferma e soft-delete.

Il customer_id del contesto arriva dal backend; non dal JSON dell'AI.
Il repository esegue la scrittura condizionata a tenant/customer/stato.
"""
from __future__ import annotations

from app.context.models import (Appointment, BookingStatus, ConversationContext,
    ConversationStep, OperationType, PendingAction, SystemResult)
from app.flows.common import reset_for_new_operation
from app.repositories import appointment as repo
from app.web.sse import notify_agenda


def identify_target(context: ConversationContext, tenant: dict, **_ignored):
    context = context.model_copy(deep=True)
    if context.conversation.current_step == ConversationStep.COMPLETED:
        context = reset_for_new_operation(context)
        context.operation.type = OperationType.CANCEL
        context.conversation.current_operation = OperationType.CANCEL

    if not context.customer.id:
        return context, SystemResult(success=False, error_code='CUSTOMER_NOT_IDENTIFIED')

    try:
        rows = repo.list_upcoming_for_customer(
            tenant['id'], context.customer.id, limit=5, tz_name=tenant.get('timezone'))
    except Exception as exc:
        print(f'[flows.cancel] lookup errore: {type(exc).__name__}')
        return context, SystemResult(success=False, error_code='TECHNICAL_ERROR')
    if not rows:
        return context, SystemResult(success=False, error_code='NO_UPCOMING_APPOINTMENTS')

    context.appointments = [Appointment(
        id=r['id'], date=r['appointment_date'], time=r['appointment_time'],
        person_name=r.get('person_name'), service=r.get('service'),
        status=BookingStatus.CONFIRMED) for r in rows]

    context.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
    if len(context.appointments) == 1:
        context.operation.target_appointment_id = context.appointments[0].id
        context.confirmation.required = True
        context.confirmation.confirmation_type = 'cancel_target'
        context.conversation.pending_action = PendingAction.CONFIRM
    else:
        context.operation.target_appointment_id = None
        context.confirmation.required = False
        context.confirmation.confirmation_type = None
        context.conversation.pending_action = PendingAction.SELECT_APPOINTMENT
    return context, SystemResult(success=True)


def appointment_selected(context: ConversationContext, **_ignored):
    context = context.model_copy(deep=True)
    target = next((a for a in context.appointments
                   if a.id == context.selected_appointment_id), None)
    if target is None:
        return context, SystemResult(success=False, error_code='NO_APPOINTMENT_TARGET')
    context.appointments = [target]
    context.operation.target_appointment_id = target.id
    context.selected_appointment_id = None
    context.confirmation.required = True
    context.confirmation.confirmation_type = 'cancel_target'
    context.conversation.current_step = ConversationStep.IDENTIFY_APPOINTMENT
    context.conversation.pending_action = PendingAction.CONFIRM
    return context, SystemResult(success=True)


def confirm(context: ConversationContext, tenant: dict, **_ignored):
    context = context.model_copy(deep=True)
    customer_id, target_id = context.customer.id, context.operation.target_appointment_id
    if context.confirmation.confirmation_type != 'cancel_target' or not customer_id or not target_id:
        return context, SystemResult(success=False, error_code='NO_APPOINTMENT_TARGET')

    context.conversation.current_step = ConversationStep.EXECUTING
    try:
        updated = repo.cancel_owned_future_appointment(
            tenant['id'], customer_id, target_id, tz_name=tenant.get('timezone'))
    except Exception as exc:
        print(f'[flows.cancel] cancellazione errore: {type(exc).__name__}')
        return context, SystemResult(success=False, error_code='TECHNICAL_ERROR')
    if not updated:
        return context, SystemResult(success=False, error_code='APPOINTMENT_NOT_FOUND')

    context.booking.id = target_id
    context.booking.status = BookingStatus.CANCELLED
    context.conversation.current_step = ConversationStep.COMPLETED
    context.conversation.pending_action = PendingAction.NONE
    context.confirmation.required = False
    notify_agenda(tenant['id'])
    return context, SystemResult(success=True, data={'appointment_id': target_id})


def reject_confirmation(context: ConversationContext, **_ignored):
    context = context.model_copy(deep=True)
    context.confirmation.required = False
    context.confirmation.confirmation_type = None
    context.operation.target_appointment_id = None
    context.conversation.current_step = ConversationStep.IDLE
    context.conversation.pending_action = PendingAction.NONE
    return context, SystemResult(success=True, data={'next': 'abandoned'})
