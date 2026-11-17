"""Riduce JSON non fidato a un solo segnale compatibile con i flow originali.

Non esegue e non legge il database. Le decisioni operative rimangono
nel router e nei servizi già presenti. In dubbio non effettua scritture.
"""
from __future__ import annotations

from app.ai.schemas import AIEnvelope
from app.context.models import AI1Result, Intent

_PRIMARY_OPERATIONS = {Intent.BOOK, Intent.RESCHEDULE, Intent.CANCEL}
_SECONDARY = {Intent.ASK_INFORMATION, Intent.GREETING, Intent.THANKS}
_CONTEXTUAL = {Intent.PROVIDE_DATA, Intent.SELECT_SLOT, Intent.SELECT_APPOINTMENT, Intent.ABANDON,
               Intent.CONFIRM, Intent.REJECT, Intent.CHANGE_PREFERENCE}


def to_legacy_signal(envelope: AIEnvelope, ai_context: dict) -> AI1Result:
    """Nessuna istruzione AI può diventare accesso diretto a Supabase."""
    def clarification(reason: str) -> AI1Result:
        return AI1Result(intent=Intent.UNKNOWN, entities={}, confidence=0.0,
                         needs_clarification=True, clarification_reason=reason)

    requests = envelope.requests

    # I messaggi puramente sociali possono non contenere richieste.
    # Mai trasformare un ringraziamento in una conferma, cancellazione o
    # abbandono: le operazioni esplicite hanno sempre la precedenza.
    social_intents = {Intent.THANKS, Intent.GREETING, Intent.UNKNOWN}
    social_only = not requests or all(r.intent in social_intents for r in requests)
    if social_only and envelope.dialogue_act in {"THANKS", "CLOSING", "ACKNOWLEDGMENT"}:
        return AI1Result(intent=Intent.THANKS, entities={}, confidence=1.0)
    if social_only and envelope.dialogue_act == "GREETING":
        return AI1Result(intent=Intent.GREETING, entities={}, confidence=1.0)

    if envelope.needs_clarification:
        return clarification(envelope.clarification_reason or "richiesta_ambigua")
    if not requests:
        return clarification("messaggio_non_classificato")
    op_intents = {r.intent for r in requests if r.intent in _PRIMARY_OPERATIONS}
    if len(op_intents) > 1 or sum(r.intent in _PRIMARY_OPERATIONS for r in requests) > 1:
        return clarification("richieste_operative_in_conflitto")

    if any(r.intent == Intent.ABANDON for r in requests) and op_intents:
        return clarification("abbandono_e_operazione_in_conflitto")

    # Un 'sì' non prevale su una correzione in corso; richiediamo di chiarire.
    if any(r.intent == Intent.CONFIRM for r in requests) and len(requests) > 1:
        return clarification("conferma_con_modifiche_o_altre_richieste")

    # Solo un'operazione per turno: le domande secondarie potranno essere
    # trattate da un successivo message composer, senza autorizzare scritture.
    prioritized = [r for r in requests if r.intent in _PRIMARY_OPERATIONS]
    if not prioritized:
        prioritized = [r for r in requests if r.intent in _CONTEXTUAL]
    if not prioritized:
        prioritized = [r for r in requests if r.intent in _SECONDARY]
    chosen = prioritized[0] if prioritized else requests[0]

    if chosen.intent == Intent.CONFIRM and not ai_context.get("confirmation_required"):
        return clarification("nessuna_conferma_pendente")

    # Se la richiesta principale è una nuova prenotazione, preferenze
    # aggiunte da CHANGE_PREFERENCE nello stesso messaggio vengono combinate,
    # ma mai sovrascritte se sono esplicitamente contraddittorie.
    entities = chosen.entities.model_dump(exclude_none=True)
    if chosen.intent == Intent.BOOK:
        for request in requests:
            if request.intent == Intent.CHANGE_PREFERENCE:
                for key, value in request.entities.model_dump(exclude_none=True).items():
                    if key in entities and entities[key] != value:
                        return clarification("preferenze_contraddittorie")
                    entities[key] = value

    secondary_info = [r.topic for r in requests if r.intent == Intent.ASK_INFORMATION and r is not chosen]
    return AI1Result(intent=chosen.intent, entities=entities, confidence=1.0,
                     context_updates={"secondary_questions": [q for q in secondary_info if q]},
                     needs_clarification=False)
