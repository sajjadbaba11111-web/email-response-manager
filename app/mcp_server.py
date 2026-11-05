"""MCP server exposing the email and WhatsApp capabilities.

The tools are mounted inside the FastAPI application at ``/mcp`` and are
protected by the ``MCP_API_KEY`` bearer token (see the middleware in
``app.main``).

The tool functions are plain module level coroutines so that they can be
tested directly; :func:`create_mcp_server` registers them on a fresh
:class:`MCPServer` instance (the streamable HTTP session manager may only be
started once per instance).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings

from app import db
from app.config import get_settings
from app.gmail_client import Email, get_gmail_client
from app.llm import get_llm
from app.whatsapp import get_whatsapp_client

logger = logging.getLogger(__name__)

SERVER_NAME = "email-whatsapp-agent"
SERVER_INSTRUCTIONS = (
    "Personal assistant connected to a Gmail mailbox and a WhatsApp number. "
    "Use list_emails/read_email to inspect the mailbox, draft_reply to prepare a "
    "reply for the owner's approval, send_email_reply to actually send a reply "
    "inside the original thread, send_whatsapp to message the owner, and "
    "list_pending_drafts to inspect drafts waiting for approval."
)


def _email_to_dict(email: Email) -> dict[str, Any]:
    return {
        "email_id": email.email_id,
        "thread_id": email.thread_id,
        "sender": email.sender,
        "subject": email.subject,
        "date": email.date,
        "message_id": email.message_id,
        "body": email.body,
    }


async def list_emails(query: str = "is:unread in:inbox", limit: int = 10) -> list[dict[str, Any]]:
    """List emails in the mailbox.

    Args:
        query: Gmail search query, e.g. ``is:unread in:inbox`` or ``from:boss@example.com``.
        limit: Maximum number of messages to return (1-50).

    Returns:
        A list of messages with sender, subject, date and a body preview.
    """

    gmail = get_gmail_client()
    limit = max(1, min(int(limit), 50))
    message_ids = await gmail.list_message_ids(query, limit)
    emails: list[Email] = []
    for message_id in message_ids:
        try:
            emails.append(await gmail.get_email(message_id))
        except Exception as exc:
            logger.warning("Could not read an email", extra={"email_id": message_id})
            logger.debug("Email read error", exc_info=exc)
    return [_email_to_dict(email) for email in emails]


async def read_email(email_id: str) -> dict[str, Any]:
    """Read a single email by its Gmail message id.

    Args:
        email_id: The Gmail message id (as returned by ``list_emails``).

    Returns:
        The full message including the plain text body.
    """

    gmail = get_gmail_client()
    email = await gmail.get_email(email_id)
    return _email_to_dict(email)


async def draft_reply(email_id: str) -> dict[str, Any]:
    """Ask Claude for a reply draft and store it as a pending draft.

    The draft is **not** sent.  It stays pending until the owner approves it
    on WhatsApp with ``send <id>``.

    Args:
        email_id: The Gmail message id to draft a reply for.

    Returns:
        The draft including its numeric id, or a note that no reply is needed.
    """

    gmail = get_gmail_client()
    llm = get_llm()

    existing = db.get_draft_for_email(email_id)
    if existing is not None:
        return {
            "draft_id": existing.id,
            "status": existing.status,
            "sender": existing.sender,
            "subject": existing.subject,
            "draft_text": existing.draft_text,
            "note": "a draft already exists for this email",
        }

    email = await gmail.get_email(email_id)
    result = await llm.draft_reply(email)

    if result.status == "no_reply":
        return {"draft_id": None, "status": "no_reply", "note": "no reply is needed"}
    if result.status == "suspicious":
        return {
            "draft_id": None,
            "status": "suspicious",
            "reason": result.reason,
            "note": "the email looks suspicious, no draft was created",
        }

    draft_id = db.create_draft(
        email_id=email.email_id,
        thread_id=email.thread_id,
        sender=email.sender,
        subject=email.subject,
        message_id_header=email.message_id,
        draft_text=result.text,
    )
    return {
        "draft_id": draft_id,
        "status": "pending",
        "sender": email.sender,
        "subject": email.subject,
        "draft_text": result.text,
        "note": "stored as pending; it is only sent after the owner approves it",
    }


async def send_email_reply(email_id: str, text: str) -> dict[str, Any]:
    """Send a reply to the sender of an email, inside the original thread.

    The reply only goes to the original sender: no CC, no BCC.

    Args:
        email_id: The Gmail message id being replied to.
        text: The body of the reply.

    Returns:
        A confirmation with the Gmail id of the sent message.
    """

    gmail = get_gmail_client()
    email = await gmail.get_email(email_id)
    message_id = await gmail.send_reply(email, text)
    logger.info("MCP tool sent an email reply", extra={"email_id": email_id})
    return {
        "sent": True,
        "email_id": email_id,
        "thread_id": email.thread_id,
        "to": email.sender,
        "gmail_message_id": message_id,
    }


async def send_whatsapp(text: str) -> dict[str, Any]:
    """Send a WhatsApp message to the owner.

    Args:
        text: The message body. Long messages are split automatically.

    Returns:
        A confirmation of the delivery.
    """

    client = get_whatsapp_client()
    settings = get_settings()
    await client.send_text(text, settings.my_whatsapp_number)
    return {"sent": True, "to": settings.my_whatsapp_number, "characters": len(text)}


async def list_pending_drafts() -> list[dict[str, Any]]:
    """List the reply drafts that are still waiting for the owner's approval.

    Returns:
        The pending drafts with their ids, senders, subjects and draft text.
    """

    return [
        {
            "draft_id": draft.id,
            "email_id": draft.email_id,
            "sender": draft.sender,
            "subject": draft.subject,
            "draft_text": draft.draft_text,
            "created_at": draft.created_at,
        }
        for draft in db.list_pending_drafts()
    ]


#: The functions exposed as MCP tools.
TOOL_FUNCTIONS = (
    list_emails,
    read_email,
    draft_reply,
    send_email_reply,
    send_whatsapp,
    list_pending_drafts,
)


def create_mcp_server() -> MCPServer:
    """Create an MCP server with every tool registered."""

    server: MCPServer = MCPServer(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    for function in TOOL_FUNCTIONS:
        server.tool()(function)
    return server


def create_mcp_asgi_app(server: MCPServer) -> StreamableHTTPASGIApp:
    """Return the path agnostic ASGI view of an MCP server.

    DNS rebinding protection is disabled on purpose: the endpoint is protected
    by a bearer token and has to be reachable through a tunnel (ngrok) or a
    reverse proxy, so the ``Host`` header cannot be restricted to localhost.
    """

    server.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return StreamableHTTPASGIApp(server.session_manager)
