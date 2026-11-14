"""Parsing webhook Meta puro: scorre ogni entry/change senza fidarsi del payload."""
from datetime import datetime, timezone


def extract_messages(payload: dict) -> list[dict]:
    messages = []
    seen = set()
    for entry in payload.get('entry', []):
        for change in entry.get('changes', []):
            value = change.get('value') or {}
            business_phone = (value.get('metadata') or {}).get('display_phone_number')
            for msg in value.get('messages') or []:
                msg_id = msg.get('id')
                if not msg_id or msg_id in seen or not msg.get('from') or not business_phone:
                    continue
                seen.add(msg_id)
                msg_type = msg.get('type')
                reply_id = None
                if msg_type == 'text':
                    body = (msg.get('text') or {}).get('body', '')
                elif msg_type == 'interactive':
                    reply = ((msg.get('interactive') or {}).get('button_reply')
                             or (msg.get('interactive') or {}).get('list_reply') or {})
                    body = reply.get('title', '')
                    reply_id = reply.get('id')
                else:
                    continue
                if not isinstance(body, str) or not body.strip():
                    continue
                try:
                    received = datetime.fromtimestamp(int(msg['timestamp']), tz=timezone.utc)
                except (KeyError, TypeError, ValueError, OverflowError):
                    received = datetime.now(timezone.utc)
                messages.append({
                    'to': business_phone,
                    'from': msg['from'],
                    'message': body.strip(),
                    'message_id': msg_id,
                    'button_reply_id': reply_id,
                    'received_at': received.isoformat(),
                })
    return messages
