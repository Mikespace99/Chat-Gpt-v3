"""Interprete conversazionale: JSON verificato, nessun accesso al DB.

Prompt esterno aggiornabile (deploy Render); schema Python non alterabile
attraverso il prompt. In caso di risposta non valida non si esegue nulla.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.ai.schemas import AIEnvelope
from app.config import Config
from app.context.models import AI1Result, Intent
from app.controller.interpretation_policy import to_legacy_signal
from app.utils.it_dates import ITALIAN_MONTHS, ITALIAN_WEEKDAYS

PROMPT_FILE = Path(__file__).parent / "prompts" / "interpreter.md"


def load_interpreter_instructions() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def _today_it(tz_name: str | None) -> tuple[str, str]:
    try:
        tz = ZoneInfo(tz_name or "Europe/Rome")
    except Exception:
        tz = ZoneInfo("Europe/Rome")
    now = datetime.now(tz)
    return (f"{ITALIAN_WEEKDAYS[now.isoweekday() % 7]} {now.day} "
            f"{ITALIAN_MONTHS[now.month - 1]} {now.year}", now.date().isoformat())


def interpret_json(raw: dict, ai_context: dict) -> AI1Result:
    """Punto testabile senza modello esterno: validazione e policy."""
    return to_legacy_signal(AIEnvelope.model_validate(raw), ai_context)


def run_ai1_interpreter(message_text: str, ai1_input: dict,
                        tz_name: str | None = None) -> AI1Result:
    from openai import OpenAI
    human, today = _today_it(tz_name)
    payload = {"oggi": human, "oggi_iso": today, "message": message_text,
               "context": ai1_input}
    try:
        # LLM senza tools: può solo restituire una descrizione JSON.
        completion = OpenAI(api_key=Config.OPENAI_API_KEY).chat.completions.create(
            model=Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": load_interpreter_instructions()},
                {"role": "system", "content": "Schema JSON obbligatorio: "
                 + json.dumps(AIEnvelope.model_json_schema(), ensure_ascii=False)},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = json.loads(completion.choices[0].message.content or "")
        return interpret_json(raw, ai1_input)
    except Exception as exc:
        # Nessun fallback che simuli un consenso, un dato o un appuntamento.
        print(f"[AI INTERPRETER] risposta non valida o errore: {type(exc).__name__}")
        return AI1Result(intent=Intent.UNKNOWN, entities={}, confidence=0.0,
                         needs_clarification=True,
                         clarification_reason="interpretazione_non_disponibile")
