"""WhatsApp Cloud API webhook (verification + inbound messages)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import get_settings, normalize_number
from app.db import mark_wa_message_processed
from app.services import handle_command
from app.whatsapp import get_whatsapp_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["whatsapp"])

#: Path the Meta webhook must be pointed at.
WEBHOOK_PATH = "/webhook/whatsapp"


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A single inbound WhatsApp text message."""

    message_id: str
    sender: str
    text: str


def verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Validate the ``X-Hub-Signature-256`` header of a webhook delivery."""

    if not signature or not secret:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature[len("sha256=") :].strip())


def extract_messages(payload: dict[str, Any]) -> list[InboundMessage]:
    """Extract the inbound text messages of a webhook delivery.

    Non-text events and status updates are ignored.
    """

    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                if message.get("type") != "text":
                    continue
                text = (message.get("text") or {}).get("body", "")
                if not isinstance(text, str) or not text.strip():
                    continue
                messages.append(
                    InboundMessage(
                        message_id=str(message.get("id", "")),
                        sender=str(message.get("from", "")),
                        text=text,
                    )
                )
    return messages


def is_allowed_sender(sender: str) -> bool:
    """Only the owner's number may drive the assistant."""

    settings = get_settings()
    return bool(sender) and normalize_number(sender) == settings.my_whatsapp_number


@router.get(WEBHOOK_PATH, summary="Webhook verification handshake")
async def verify_webhook(request: Request) -> PlainTextResponse:
    """Meta echoes ``hub.challenge`` when ``hub.verify_token`` matches."""

    settings = get_settings()
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token and token == settings.whatsapp_verify_token:
        logger.info("Webhook verification succeeded")
        return PlainTextResponse(challenge or "", status_code=200)

    logger.warning("Webhook verification failed")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post(WEBHOOK_PATH, summary="Inbound WhatsApp messages")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, str]:
    """Accept a webhook delivery and process it in the background."""

    settings = get_settings()
    body = await request.body()

    if not verify_signature(
        body, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret
    ):
        logger.warning("Rejected a webhook delivery with an invalid signature")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid payload")

    accepted = 0
    for message in extract_messages(payload):
        if not is_allowed_sender(message.sender):
            logger.warning(
                "Ignored a message from an unknown number",
                extra={"sender_suffix": message.sender[-4:]},
            )
            continue
        if not mark_wa_message_processed(message.message_id):
            logger.info(
                "Ignored a duplicate webhook delivery", extra={"message_id": message.message_id}
            )
            continue
        accepted += 1
        background_tasks.add_task(process_inbound_message, message)

    return {"status": "ok", "accepted": str(accepted)}


async def process_inbound_message(message: InboundMessage) -> None:
    """Execute a command that arrived over WhatsApp and answer it."""

    try:
        reply = await handle_command(message.text, sender=message.sender)
        if reply:
            await get_whatsapp_client().send_text(reply)
    except Exception:
        logger.exception("Failed to process an inbound WhatsApp message")
