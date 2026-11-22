
"""
AI Booking — agente unico
====================

Punto di ingresso HTTP:
- Dashboard web
- Health check
- Verifica webhook WhatsApp (GET)
- Ricezione webhook WhatsApp (POST)
- Inbox persistente Supabase
- Worker asincrono

L'AI non accede direttamente a Supabase.
Le operazioni sono gestite dal backend.
"""

import asyncio
import hashlib
import hmac
import json
import logging

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app.config import Config
from app.messaging import store
from app.messaging.worker import worker_loop
from app.messaging_parser import extract_messages
from app.web.routes import router as web_router


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("ai_booking")


# ============================================================
# LIFESPAN / WORKER
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):

    logger.info("[STARTUP] AI Booking agente unico starting")

    stop_event = asyncio.Event()

    worker_task = asyncio.create_task(
        worker_loop(stop_event)
    )

    logger.info("[STARTUP] WhatsApp worker started")

    try:
        yield

    finally:
        logger.info("[SHUTDOWN] Stopping WhatsApp worker")

        stop_event.set()

        try:
            await asyncio.wait_for(
                worker_task,
                timeout=15,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[SHUTDOWN] Worker timeout; cancelling"
            )
            worker_task.cancel()
            await asyncio.gather(
                worker_task,
                return_exceptions=True,
            )

        logger.info("[SHUTDOWN] Complete")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="AI Booking",
    version="5.0.0",
    lifespan=lifespan,
)

app.include_router(web_router)


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "version": "5.0.0",
    }


@app.get("/api/status")
async def api_status():

    return {
        "status": "running",
        "message": "Backend WhatsApp AI attivo",
    }


# ============================================================
# META SIGNATURE VALIDATION
# ============================================================

def _verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    """
    Verifica X-Hub-Signature-256 con HMAC SHA256.

    Importante:
    - Utilizza i byte originali della richiesta.
    - Non modifica il JSON prima della verifica.
    - Non registra mai firme o segreti nei log.
    """

    if not app_secret:
        return False

    if not signature_header:
        return False

    if not signature_header.startswith("sha256="):
        return False

    received = signature_header.split("=", 1)[1]

    expected = hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        received,
    )


# ============================================================
# WHATSAPP WEBHOOK VERIFICATION (GET)
# ============================================================

@app.get("/webhook/whatsapp")
async def verify_whatsapp(request: Request):

    params = request.query_params

    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    configured_token = Config.WHATSAPP_VERIFY_TOKEN

    if (
        mode == "subscribe"
        and configured_token
        and token
        and hmac.compare_digest(
            token,
            configured_token,
        )
    ):

        logger.info(
            "[META GET] Webhook verification successful"
        )

        return PlainTextResponse(
            challenge or "",
            status_code=200,
        )

    logger.warning(
        "[META GET] Verification rejected"
    )

    return PlainTextResponse(
        "Forbidden",
        status_code=403,
    )


# ============================================================
# WHATSAPP WEBHOOK (POST)
# ============================================================

@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):

    # --------------------------------------------------------
    # 1. RAW BODY
    # --------------------------------------------------------

    raw_body = await request.body()

    signature = request.headers.get(
        "x-hub-signature-256"
    )

    app_secret = Config.WHATSAPP_APP_SECRET

    logger.info(
    "[SECRET CHECK] length=%s whitespace=%s",
    len(app_secret or ""),
    bool(app_secret and app_secret != app_secret.strip()),
    )

    # --------------------------------------------------------
    # 2. DIAGNOSTICA SICURA
    # --------------------------------------------------------

    logger.info(
        "[META DEBUG] "
        "secret_configured=%s "
        "signature_present=%s "
        "body_bytes=%s",
        bool(app_secret),
        bool(signature),
        len(raw_body),
    )

    # --------------------------------------------------------
    # 3. APP SECRET OBBLIGATORIO
    # --------------------------------------------------------

    if not app_secret:

        logger.error(
            "[META ERROR] WHATSAPP_APP_SECRET missing"
        )

        return PlainTextResponse(
            "Webhook non configurato",
            status_code=503,
        )

    # --------------------------------------------------------
    # 4. VERIFICA FIRMA META
    # --------------------------------------------------------

    signature_valid = _verify_meta_signature(
        raw_body=raw_body,
        signature_header=signature,
        app_secret=app_secret,
    )

    logger.info(
        "[META DEBUG] signature_valid=%s",
        signature_valid,
    )

    if not signature_valid:

        logger.warning(
            "[META ERROR] Invalid webhook signature"
        )

        return PlainTextResponse(
            "Forbidden",
            status_code=403,
        )

    # --------------------------------------------------------
    # 5. PARSING JSON
    # --------------------------------------------------------

    try:

        payload = json.loads(raw_body)

    except (json.JSONDecodeError, UnicodeDecodeError):

        logger.warning(
            "[META ERROR] Invalid JSON payload"
        )

        return PlainTextResponse(
            "Bad Request",
            status_code=400,
        )

    # --------------------------------------------------------
    # 6. ESTRAZIONE MESSAGGI
    # --------------------------------------------------------

    try:

        messages = extract_messages(payload)

    except (ValueError, TypeError, KeyError) as exc:

        logger.warning(
            "[META ERROR] Message parsing failed: %s",
            type(exc).__name__,
        )

        return PlainTextResponse(
            "Bad Request",
            status_code=400,
        )

    if not messages:

        logger.info(
            "[META POST] No incoming messages; event ignored"
        )

        return {
            "status": "ignored",
        }

    logger.info(
        "[META POST] Received %s message(s)",
        len(messages),
    )

    # --------------------------------------------------------
    # 7. SALVATAGGIO INBOX SUPABASE
    # --------------------------------------------------------

    try:

        for message in messages:

            await asyncio.to_thread(
                store.enqueue,
                message,
            )

        logger.info(
            "[META INBOX] %s message(s) queued",
            len(messages),
        )

    except Exception:

        logger.exception(
            "[META INBOX ERROR] Persistent enqueue failed"
        )

        # Non confermiamo a Meta la ricezione se
        # il salvataggio persistente è fallito.

        return PlainTextResponse(
            "Temporarily unavailable",
            status_code=503,
        )

    # --------------------------------------------------------
    # 8. ACK A META
    # --------------------------------------------------------

    logger.info(
        "[META POST] Accepted successfully"
    )

    return {
        "status": "accepted",
        "count": len(messages),
    }
