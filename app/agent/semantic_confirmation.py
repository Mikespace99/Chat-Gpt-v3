"""Semantic understanding of pending confirmations; NEVER grants DB authorization.

The controller decides if a pending proposal is valid and whether a returned
ACCEPT may be executed. Model failures fail closed, with no implicit assent.
"""
from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool, model_validator

from app.agent.settings import AgentSettings, load_settings
from app.config import Config
from app.context.models import ConversationContext
from app.controller.confirmation import is_valid_confirmation

LOG = logging.getLogger(__name__)
PROMPT_FILE = Path(__file__).with_name("confirmation_prompt.md")


class ConfirmationDecision(str, Enum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    MODIFY = "MODIFY"
    UNCERTAIN = "UNCERTAIN"
    UNRELATED = "UNRELATED"


class SemanticConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: ConfirmationDecision
    unconditional: StrictBool

    @model_validator(mode="after")
    def validate_unconditional(self):
        if self.decision != ConfirmationDecision.ACCEPT and self.unconditional:
            raise ValueError("Only unqualified ACCEPT may be unconditional")
        return self


def interpret_confirmation(context: ConversationContext, message_text: str,
                           *, settings: AgentSettings | None = None, client=None
                           ) -> SemanticConfirmation:
    """Only classifies the customer's message, never authorizes a mutation.

    Returns UNCERTAIN if the model is unavailable or output is malformed.
    This is deliberately not a conversational tool loop.
    """
    unsure = SemanticConfirmation(decision=ConfirmationDecision.UNCERTAIN, unconditional=False)
    cfg = settings or load_settings()
    if not cfg.confirmation_semantic_enabled or not is_valid_confirmation(context):
        return unsure
    if not message_text.strip() or len(message_text) > 2500:
        return unsure
    payload = {
        "phase": "AWAITING_CONFIRMATION",
        "operation": context.operation.type.value,
        "confirmation_type": context.confirmation.confirmation_type,
        "proposal": {
            "new_slot": next((
                {"date": s.slot.date.isoformat(), "time": s.slot.time.strftime("%H:%M")}
                for s in context.offered_slots if s.slot.id == context.booking.slot_id
            ), None),
            "is_proposal_pending": True,
        },
        "customer_message": message_text,
    }
    try:
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=Config.OPENAI_API_KEY, timeout=cfg.confirmation_timeout_seconds)
        answer = client.chat.completions.create(
            model=cfg.model or Config.AI_MODEL_INTENT,
            messages=[
                {"role": "system", "content": PROMPT_FILE.read_text(encoding="utf-8")},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=cfg.confirmation_max_output_tokens,
        )
        parsed = SemanticConfirmation.model_validate_json(answer.choices[0].message.content or "")
        # An inconsistent ACCEPT is not an authorization.
        if parsed.decision == ConfirmationDecision.ACCEPT and not parsed.unconditional:
            return unsure
        return parsed
    except Exception as exc:
        LOG.warning("[semantic_confirmation] classification unavailable: %s", type(exc).__name__)
        return unsure
