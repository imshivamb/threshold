"""POST /webhooks/ring — Ring signs the raw body with HMAC-SHA256; verify before parsing."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from db import sessionmaker
from ingest import store_webhook

log = logging.getLogger("threshold.webhooks")
router = APIRouter()

SIGNATURE_HEADER = "X-Signature"


def verify_signature(key: str, raw_body: bytes, received: str | None) -> bool:
    """Constant-time check. Accepts the bare hex digest or the ``sha256=<hex>`` form."""
    if not received or not key:
        return False
    received = received.strip()
    if received.lower().startswith("sha256="):
        received = received[len("sha256=") :]
    expected = hmac.new(key.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.lower())


@router.post("/webhooks/ring")
async def ring_webhook(request: Request) -> JSONResponse:
    ctx = request.app.state.ctx
    raw = await request.body()
    if not verify_signature(ctx.settings.ring_webhook_signing_key, raw, request.headers.get(SIGNATURE_HEADER)):
        log.warning("rejected webhook: bad signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    try:
        payload = json.loads(raw)
        request_id = payload["meta"]["request_id"]
        payload["data"]["type"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"error": "malformed payload"}, status_code=400)

    source = "emulator" if ctx.settings.ring_mode == "emulator" else "webhook"
    async with sessionmaker()() as session:
        stored = await store_webhook(session, ctx.household_id, payload, source=source)
        await session.commit()
    if not stored:
        log.info("duplicate webhook %s dropped", request_id)
        return JSONResponse({"status": "duplicate", "request_id": request_id})
    return JSONResponse({"status": "stored", "request_id": request_id})
