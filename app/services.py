"""Business logic of the assistant.

This module owns every side effect:

* reading Gmail,
* asking Claude for drafts and summaries,
* sending WhatsApp messages,
* executing the commands received over WhatsApp.

The single hard rule of the project lives here: **an email is only ever sent
because the owner explicitly asked for it over WhatsApp**.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from app import db
from app.config import get_settings, normalize_number
from app.gmail_client import Email, get_gmail_client
from app.llm import DraftResult, get_llm
from app.whatsapp import (
    WhatsAppClient,
    WhatsAppTemplateRequiredError,
    get_whatsapp_client,
)

logger = logging.getLogger(__name__)

#: Gmail query used by the poller.
QUERY_NEW_MAIL = "is:unread in:inbox newer_than:1d"
#: Gmail query used by the daily summary.
QUERY_DAILY = "newer_than:1d in:inbox"

#: ``app_state`` key marking that the initial unread mail was seeded.
STATE_SEEDED = "seeded_initial_unread"

#: Last time an error notice was sent per error key (in-memory rate limit).
_last_error_notice: dict[str, float] = {}

_SEND_RE = re.compile(r"^send\s+(\d+)(?:\s(.*))?$", re.IGNORECASE | re.DOTALL)
_DISCARD_RE = re.compile(r"^discard\s+(\d+)\s*$", re.IGNORECASE | re.DOTALL)
_EXACT_RE = re.compile(r"^(drafts|summary|help)\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Command:
    """A parsed WhatsApp command."""

    kind: str
    draft_id: int | None = None
    body: str = ""


def parse_command(text: str) -> Command:
    """Parse a raw WhatsApp message into a :class:`Command`.

    ``send <id>`` sends the stored draft, ``send <id> <text>`` sends the
    owner's own wording.  The edited text is preserved verbatim (including
    inner newlines).
    """

    raw = (text or "").strip()

    match = _SEND_RE.match(raw)
    if match:
        draft_id = int(match.group(1))
        body = (match.group(2) or "").strip()
        return Command(kind="send_edited" if body else "send", draft_id=draft_id, body=body)

    match = _DISCARD_RE.match(raw)
    if match:
        return Command(kind="discard", draft_id=int(match.group(1)))

    match = _EXACT_RE.match(raw)
    if match:
        return Command(kind=match.group(1).lower())

    return Command(kind="unknown")


# --------------------------------------------------------------------------- #
# message rendering
# --------------------------------------------------------------------------- #
def render_new_email_message(draft_id: int, email: Email, draft_text: str) -> str:
    """Render the WhatsApp message announcing a new draft."""

    return (
        "📧 *New email*\n"
        f"From: {email.sender}\n"
        f"Subject: {email.subject or '(no subject)'}\n"
        "\n"
        f"Preview: {email.preview(240)}\n"
        "\n"
        f"*Draft reply (id {draft_id}):*\n"
        f"{draft_text}\n"
        "\n"
        f"Reply `send {draft_id}` to send it as-is,\n"
        f"`send {draft_id} <your text>` to send your own version,\n"
        f"or `discard {draft_id}` to drop it."
    )


def render_suspicious_message(email: Email, reason: str) -> str:
    """Render the warning sent when an email looks like a scam."""

    return (
        "⚠️ *Suspicious email - not drafted*\n"
        f"From: {email.sender}\n"
        f"Subject: {email.subject or '(no subject)'}\n"
        f"Preview: {email.preview(240)}\n"
        "\n"
        f"Reason: {reason}\n"
        "\n"
        "No draft was created. Check the email in Gmail yourself."
    )


def render_pending_drafts(drafts: list[db.Draft]) -> str:
    """Render the ``drafts`` command output."""

    if not drafts:
        return "No pending drafts."
    lines = [f"*{len(drafts)} pending draft(s):*"]
    for draft in drafts:
        preview = " ".join(draft.draft_text.split())[:120]
        lines.append(
            f"\n#{draft.id} from {draft.sender}\n"
            f"Subject: {draft.subject or '(no subject)'}\n"
            f"Draft: {preview}"
        )
    lines.append("\nReply `send <id>` or `discard <id>`.")
    return "\n".join(lines)


HELP_TEXT: str = (
    "*Commands*\n"
    "`send <id>` - send draft <id> exactly as written\n"
    "`send <id> <your text>` - send draft <id> with your own wording\n"
    "`discard <id>` - drop draft <id>\n"
    "`drafts` - list the drafts waiting for you\n"
    "`summary` - send the daily email summary now\n"
    "`help` - show this message"
)


# --------------------------------------------------------------------------- #
# error notices
# --------------------------------------------------------------------------- #
async def notify_error(context: str, exc: BaseException) -> None:
    """Send a short WhatsApp error notice, rate limited per error key."""

    settings = get_settings()
    key = context
    now = time.monotonic()
    cooldown = settings.error_notice_cooldown_minutes * 60
    last = _last_error_notice.get(key)
    if last is not None and now - last < cooldown:
        logger.warning("Error notice suppressed (rate limited)", extra={"context": context})
        return

    try:
        client = get_whatsapp_client()
        await client.send_text(
            f"⚠️ The assistant hit a problem while running `{context}`: "
            f"{type(exc).__name__}. Please check the logs."
        )
    except Exception:  # pragma: no cover - we are already in a failure path
        logger.exception("Could not deliver the error notice", extra={"context": context})
    finally:
        _last_error_notice[key] = now


def reset_error_notices() -> None:
    """Clear the rate limiter (used by tests)."""

    _last_error_notice.clear()


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
async def check_new_mail() -> int:
    """Poll Gmail for new mail and send a draft to WhatsApp.

    Returns the number of drafts created.  Errors never propagate: they are
    logged and reported to the owner (rate limited).
    """

    settings = get_settings()
    gmail = get_gmail_client()
    llm = get_llm()
    client = get_whatsapp_client()

    try:
        message_ids = await gmail.list_message_ids(QUERY_NEW_MAIL, settings.gmail_max_results)
        unseen = db.filter_unseen(message_ids)
        logger.info(
            "Polling Gmail",
            extra={"found": len(message_ids), "unseen": len(unseen)},
        )
        created = 0
        for email_id in unseen:
            try:
                if await _process_new_email(email_id, gmail=gmail, llm=llm, client=client):
                    created += 1
            except Exception as exc:
                logger.exception("Could not process email", extra={"email_id": email_id})
                await notify_error("check_new_mail", exc)
        return created
    except Exception as exc:
        logger.exception("check_new_mail failed")
        await notify_error("check_new_mail", exc)
        return 0


def _snapshot_email(email: Email) -> None:
    db.save_email_snapshot(
        email_id=email.email_id,
        thread_id=email.thread_id,
        sender=email.sender,
        subject=email.subject,
        message_id_header=email.message_id,
        email_date=email.date,
        body=email.body,
    )


async def _process_new_email(
    email_id: str,
    *,
    gmail: Any,
    llm: Any,
    client: WhatsAppClient,
) -> bool:
    """Draft and announce a single email.  Returns ``True`` when a draft was made."""

    existing = db.get_draft_for_email(email_id)
    if existing is not None:
        # A previous run already drafted this email but failed to notify.
        email = await gmail.get_email(email_id)
        _snapshot_email(email)
        await client.send_text(render_new_email_message(existing.id, email, existing.draft_text))
        db.mark_seen(email_id)
        db.log_activity(
            "draft_reannounced",
            f"Re-announced draft #{existing.id} for {email.sender}",
            email_id=email_id,
            draft_id=existing.id,
            actor="system",
        )
        return False

    email = await gmail.get_email(email_id)
    _snapshot_email(email)
    db.log_activity(
        "email_received",
        f"New email from {email.sender}: {email.subject or '(no subject)'}",
        email_id=email.email_id,
        actor="system",
        detail={"thread_id": email.thread_id},
    )
    result: DraftResult = await llm.draft_reply(email)

    if result.status == "no_reply":
        logger.info("No reply needed", extra={"email_id": email_id})
        db.mark_seen(email_id)
        db.log_activity(
            "no_reply_needed",
            f"No reply needed for {email.sender}: {email.subject or '(no subject)'}",
            email_id=email.email_id,
            actor="system",
        )
        return False

    if result.status == "suspicious":
        logger.warning("Suspicious email detected", extra={"email_id": email_id})
        db.mark_seen(email_id)
        db.log_activity(
            "suspicious_email",
            f"Suspicious email from {email.sender}: {result.reason}",
            email_id=email.email_id,
            actor="system",
            detail={"reason": result.reason},
        )
        await client.send_text(render_suspicious_message(email, result.reason))
        return False

    draft_id = db.create_draft(
        email_id=email.email_id,
        thread_id=email.thread_id,
        sender=email.sender,
        subject=email.subject,
        message_id_header=email.message_id,
        draft_text=result.text,
    )
    db.log_activity(
        "draft_created",
        f"Draft #{draft_id} created for {email.sender}",
        email_id=email.email_id,
        draft_id=draft_id,
        actor="assistant",
        detail={"subject": email.subject, "draft_text": result.text},
    )
    await client.send_text(render_new_email_message(draft_id, email, result.text))
    db.log_activity(
        "whatsapp_notified",
        f"Draft #{draft_id} sent to WhatsApp for approval",
        email_id=email.email_id,
        draft_id=draft_id,
        actor="system",
    )
    db.mark_seen(email_id)
    return True


async def daily_summary() -> str:
    """Summarise the last 24 hours of mail and send it to WhatsApp.

    Returns the summary text that was delivered (empty when nothing to send).
    """

    settings = get_settings()
    gmail = get_gmail_client()
    llm = get_llm()
    client = get_whatsapp_client()

    try:
        message_ids = await gmail.list_message_ids(QUERY_DAILY, settings.gmail_max_results)
        emails: list[Email] = []
        for email_id in message_ids:
            try:
                emails.append(await gmail.get_email(email_id))
            except Exception as exc:
                logger.warning("Could not read email for the summary", extra={"email_id": email_id})
                logger.debug("Email read error", exc_info=exc)

        if not emails:
            await _send_whatsapp(client, "☕ No new emails in the last 24 hours.")
            return ""

        summary = await llm.summarize(emails)
        await _send_whatsapp(client, f"📊 *Daily email summary*\n\n{summary}")
        db.log_activity(
            "daily_summary_sent",
            f"Daily summary sent for {len(emails)} email(s)",
            actor="system",
            detail={"email_count": len(emails), "summary": summary[:4000]},
        )
        return summary
    except Exception as exc:
        logger.exception("daily_summary failed")
        await notify_error("daily_summary", exc)
        return ""


async def _send_whatsapp(client: WhatsAppClient, text: str) -> None:
    """Send ``text`` and fall back to a template when the 24h window closed."""

    try:
        await client.send_text(text)
    except WhatsAppTemplateRequiredError:
        settings = get_settings()
        logger.warning(
            "Free-form WhatsApp message rejected (24h window closed); "
            "falling back to the approved template"
        )
        try:
            await client.send_template(
                None,
                settings.whatsapp_template_name,
                settings.whatsapp_template_language,
                [text],
            )
        except Exception:
            logger.exception(
                "Could not send the template fallback. A pre-approved WhatsApp template "
                "is required outside the 24 hour customer service window."
            )
            raise


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def _assert_authorised(sender: str | None) -> None:
    """Only the owner may trigger commands that send email."""

    if sender is None:
        # Trusted internal caller (MCP tool, tests, CLI).
        return
    settings = get_settings()
    if normalize_number(sender) != settings.my_whatsapp_number:
        raise PermissionError("Only the owner WhatsApp number may run commands.")


async def handle_command(text: str, sender: str | None = None) -> str:
    """Execute a WhatsApp command and return the reply to send back.

    An empty string means "nothing to reply".  Errors are turned into a
    readable message, never into an exception.
    """

    try:
        _assert_authorised(sender)
    except PermissionError as exc:
        logger.warning("Rejected command from an unauthorised sender")
        return f"❌ {exc}"

    command = parse_command(text)
    logger.info("Handling command", extra={"kind": command.kind, "draft_id": command.draft_id})
    db.log_activity(
        "whatsapp_command",
        f"WhatsApp command: {command.kind}" + (f" #{command.draft_id}" if command.draft_id else ""),
        draft_id=command.draft_id,
        actor="owner",
        detail={"kind": command.kind, "text": (text or "")[:500]},
    )

    try:
        if command.kind == "send" or command.kind == "send_edited":
            return await _handle_send(command)
        if command.kind == "discard":
            return _handle_discard(command)
        if command.kind == "drafts":
            return render_pending_drafts(db.list_pending_drafts())
        if command.kind == "summary":
            await daily_summary()
            return ""
        if command.kind == "help":
            return HELP_TEXT
        return "❓ Unknown command. Reply `help` to see the available commands."
    except Exception as exc:
        logger.exception("Command failed", extra={"kind": command.kind})
        return f"❌ That command failed: {type(exc).__name__}. Please check the logs."


async def _handle_send(command: Command) -> str:
    draft_id = command.draft_id
    assert draft_id is not None
    draft = db.get_draft(draft_id)
    if draft is None:
        return f"❌ There is no draft with id {draft_id}. Reply `drafts` to list the open ones."

    if draft.status == "sent":
        return (
            f"ℹ️ Draft {draft_id} was already sent{f' at {draft.sent_at}' if draft.sent_at else ''}."
        )
    if draft.status == "discarded":
        return f"ℹ️ Draft {draft_id} was discarded."

    final_text = command.body if command.kind == "send_edited" else draft.draft_text
    if not final_text.strip():
        return f"❌ Draft {draft_id} has no text to send."

    gmail = get_gmail_client()
    email = Email(
        email_id=draft.email_id,
        thread_id=draft.thread_id or "",
        sender=draft.sender,
        subject=draft.subject or "",
        message_id=draft.message_id_header or "",
        date="",
        body="",
    )
    await gmail.send_reply(email, final_text)

    if not db.mark_draft_sent(draft_id, final_text):
        return f"⚠️ Draft {draft_id} could not be marked as sent, but the email was sent."

    logger.info("Draft sent", extra={"draft_id": draft_id, "sender": draft.sender})
    db.log_activity(
        "reply_sent",
        f"Reply sent for draft #{draft_id} to {draft.sender}",
        email_id=draft.email_id,
        draft_id=draft_id,
        actor="owner",
        detail={
            "to": draft.sender,
            "subject": draft.subject,
            "edited": command.kind == "send_edited",
            "response_text": final_text,
        },
    )
    return f"✅ Sent reply {draft_id} to {draft.sender}."


def _handle_discard(command: Command) -> str:
    draft_id = command.draft_id
    assert draft_id is not None
    draft = db.get_draft(draft_id)
    if draft is None:
        return f"❌ There is no draft with id {draft_id}."
    if draft.status == "sent":
        return f"ℹ️ Draft {draft_id} was already sent and cannot be discarded."
    if draft.status == "discarded":
        return f"ℹ️ Draft {draft_id} was already discarded."
    db.mark_draft_discarded(draft_id)
    logger.info("Draft discarded", extra={"draft_id": draft_id})
    db.log_activity(
        "draft_discarded",
        f"Draft #{draft_id} discarded for {draft.sender}",
        email_id=draft.email_id,
        draft_id=draft_id,
        actor="owner",
    )
    return f"🗑️ Draft {draft_id} discarded."


# --------------------------------------------------------------------------- #
# startup
# --------------------------------------------------------------------------- #
async def seed_initial_unread() -> int:
    """Mark the currently unread mail as seen on the very first startup.

    This prevents the assistant from drafting a reply for every email that
    was already waiting in the inbox when the project was installed.
    """

    if db.get_state(STATE_SEEDED) is not None:
        return 0

    gmail = get_gmail_client()
    try:
        message_ids = await gmail.list_message_ids("is:unread in:inbox", 500)
    except Exception as exc:
        logger.warning("Could not seed the initial unread mail", exc_info=exc)
        return 0

    count = db.mark_seen_many(message_ids)
    db.set_state(STATE_SEEDED, str(count))
    logger.info("Seeded the initial unread mail", extra={"count": count})
    return count
