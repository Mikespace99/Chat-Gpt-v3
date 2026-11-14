"""
Punto di ingresso HTTP: webhook WhatsApp + health + UI web.
Tutta la logica conversazionale è in webhook_new_pipeline / orchestrator.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.config import Config
from app.messaging import store
from app.messaging.worker import worker_loop
from app.messaging_parser import extract_messages
from app.web.routes import router as web_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    worker = asyncio.create_task(worker_loop(stop))
    try:
        yield
    finally:
        stop.set()
        await worker


app = FastAPI(
    title="AI Booking", version="3.0.0", lifespan=lifespan,
)

app.include_router(web_router)


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0"}


@app.get("/api/status")
def api_status():
    return {"status": "running", "message": "Backend WhatsApp AI attivo"}


# ============================================================
# WHATSAPP: verifica firma Meta
# ============================================================

def _verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


# ============================================================
# WHATSAPP: verifica webhook (GET)
# ============================================================

@app.get("/webhook/whatsapp")
async def verify_whatsapp(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == Config.WHATSAPP_VERIFY_TOKEN:
        return PlainTextResponse(challenge or "")

    return PlainTextResponse("Forbidden", status_code=403)


# ============================================================
# WHATSAPP: ricezione messaggi (POST)
# ============================================================

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    raw_body = await request.body()

    # Fail-closed: su Render l'App Secret è sempre obbligatorio.
    if not Config.WHATSAPP_APP_SECRET:
        return PlainTextResponse("Webhook non configurato", status_code=503)
    if not _verify_meta_signature(
        raw_body, request.headers.get("x-hub-signature-256"),
        Config.WHATSAPP_APP_SECRET,
    ):
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        items = extract_messages(json.loads(raw_body))
        if not items:
            return {"status": "ignored"}
        # Persist before returning 200 to Meta. Never queue only in RAM.
        for message in items:
            await asyncio.to_thread(store.enqueue, message)
        return {"status": "accepted", "count": len(items)}
    except (ValueError, TypeError, KeyError) as exc:
        print(f"[WEBHOOK ERROR] invalid payload: {type(exc).__name__}")
        return PlainTextResponse("Bad Request", status_code=400)
    except Exception as exc:
        # A failed durable insert MUST NOT be acknowledged as received.
        print(f"[WEBHOOK ERROR] enqueue failed: {type(exc).__name__}")
        return PlainTextResponse("Temporarily unavailable", status_code=503)


