"""Simulatore offline dei contratti AI: mai accessi a Supabase/Meta.

Esempio:
 python -m app.simulator.cli examples/conversation_simulation.json

Il file JSON contiene una lista di turni con `message`, `ai_json` e
facoltativamente `confirmation_required`. Visualizza interpretazione e
classificazione policy; NON esegue flussi di prenotazione.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError
from app.ai.schemas import AIEnvelope
from app.controller.interpretation_policy import to_legacy_signal


def simulate_turn(turn: dict) -> dict:
    try:
        envelope = AIEnvelope.model_validate(turn['ai_json'])
        signal = to_legacy_signal(envelope, {
            'confirmation_required': bool(turn.get('confirmation_required', False)),
        })
        return {
            'message': turn['message'],
            'requests': [r.model_dump(mode='json') for r in envelope.requests],
            'controller_decision': {
                'intent': signal.intent.value,
                'entities': signal.entities,
                'requires_clarification': signal.needs_clarification,
                'reason': signal.clarification_reason,
                'secondary_questions': signal.context_updates.get('secondary_questions', []),
            },
            'side_effects': 'NONE',
        }
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        return {'message': turn.get('message', ''), 'error': type(exc).__name__,
                'side_effects': 'NONE'}


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Uso: python -m app.simulator.cli examples/conversation_simulation.json')
    turns = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    if not isinstance(turns, list):
        raise SystemExit('Il file deve contenere un array JSON di turni')
    print(json.dumps([simulate_turn(t) for t in turns], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
