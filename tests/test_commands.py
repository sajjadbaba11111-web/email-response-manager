"""Tests for WhatsApp command parsing and handling."""

from __future__ import annotations

import pytest

from app import db
from app.services import handle_command, parse_command, render_pending_drafts


def test_parse_send_plain():
    cmd = parse_command("send 12")
    assert cmd.kind == "send"
    assert cmd.draft_id == 12
    assert cmd.body == ""


def test_parse_send_edited_preserves_text_and_newlines():
    cmd = parse_command("send 7 Thanks,\nThursday works for me")
    assert cmd.kind == "send_edited"
    assert cmd.draft_id == 7
    assert cmd.body == "Thanks,\nThursday works for me"


def test_parse_discard():
    cmd = parse_command("discard 3")
    assert cmd.kind == "discard"
    assert cmd.draft_id == 3


@pytest.mark.parametrize(
    "text,kind", [("drafts", "drafts"), ("summary", "summary"), ("help", "help"), ("HELP", "help")]
)
def test_parse_exact_commands(text, kind):
    assert parse_command(text).kind == kind


def test_parse_unknown():
    assert parse_command("hello there").kind == "unknown"


async def test_handle_unknown_command():
    reply = await handle_command("nonsense")
    assert "Unknown command" in reply
    assert "help" in reply


async def test_handle_help():
    reply = await handle_command("help")
    assert "send <id>" in reply
    assert "discard <id>" in reply


async def test_handle_drafts_empty():
    reply = await handle_command("drafts")
    assert "No pending drafts" in reply


async def test_handle_drafts_lists_pending():
    db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello back",
    )
    reply = await handle_command("drafts")
    assert "#1" in reply
    assert "a@example.com" in reply
    assert "Hi" in reply


async def test_send_wrong_id():
    reply = await handle_command("send 999")
    assert "no draft with id 999" in reply.lower() or "no draft" in reply.lower()


async def test_discard_wrong_id():
    reply = await handle_command("discard 999")
    assert "no draft" in reply.lower()


async def test_send_already_sent(patch_clients):
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    db.mark_draft_sent(draft_id, "Hello")
    reply = await handle_command(f"send {draft_id}")
    assert "already sent" in reply.lower()


async def test_send_already_discarded():
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    db.mark_draft_discarded(draft_id)
    reply = await handle_command(f"send {draft_id}")
    assert "discarded" in reply.lower()


async def test_discard_already_sent():
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    db.mark_draft_sent(draft_id, "Hello")
    reply = await handle_command(f"discard {draft_id}")
    assert "already sent" in reply.lower()


async def test_discard_already_discarded():
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    db.mark_draft_discarded(draft_id)
    reply = await handle_command(f"discard {draft_id}")
    assert "already discarded" in reply.lower()


async def test_discard_success():
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    reply = await handle_command(f"discard {draft_id}")
    assert "discarded" in reply.lower()
    assert db.get_draft(draft_id).status == "discarded"


async def test_unauthorised_sender_rejected():
    draft_id = db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="Hi",
        message_id_header="<m1>",
        draft_text="Hello",
    )
    reply = await handle_command(f"send {draft_id}", sender="19999999999")
    assert "owner" in reply.lower() or "❌" in reply
    assert db.get_draft(draft_id).status == "pending"


def test_render_pending_drafts_empty():
    assert "No pending drafts" in render_pending_drafts([])
