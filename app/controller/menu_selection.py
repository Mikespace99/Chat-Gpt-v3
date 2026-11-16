"""Handle trusted WhatsApp opening-menu IDs, separately from language AI.

Menu choices choose only a *workflow*, never permission to modify data.
Unknown/stale button IDs are not interpreted as menu choices.
"""
from datetime import datetime, timezone

from app.context.models import AI1Result, ConversationStatus, Intent, Message
from app.config import Config

_MENU_INTENTS = {
    "book": Intent.BOOK,
    "reschedule": Intent.RESCHEDULE,
    "cancel": Intent.CANCEL,
}


def interpret_menu_selection(button_reply_id: str | None) -> AI1Result | None:
    intent = _MENU_INTENTS.get(button_reply_id) if button_reply_id else None
    if intent is None:
        return None
    return AI1Result(intent=intent, entities={}, confidence=1.0,
                     needs_clarification=False)


def mark_menu_presented(context, message_text: str) -> None:
    """Make the welcome menu a durable transition, even if AI is uncertain."""
    context.conversation.status = ConversationStatus.ACTIVE
    context.conversation.timeout.last_activity_at = datetime.now(timezone.utc)
    context.memory.recent_messages.append(Message(
        role="user", text=message_text, timestamp=datetime.now(timezone.utc)))
    context.memory.recent_messages = context.memory.recent_messages[-Config.MAX_RECENT_MESSAGES:]
