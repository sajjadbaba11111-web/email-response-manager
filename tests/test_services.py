"""Tests for core business logic (new mail flow, summaries, send approval)."""

from __future__ import annotations

from app import db
from app.llm import DraftResult
from app.services import (
    STATE_SEEDED,
    check_new_mail,
    daily_summary,
    handle_command,
    seed_initial_unread,
)


async def test_check_new_mail_creates_draft_and_notifies(patch_clients, sample_email):
    _gmail, wa, llm = patch_clients
    llm.draft_result = DraftResult(status="draft", text="Sure, lunch works.")

    created = await check_new_mail()

    assert created == 1
    drafts = db.list_pending_drafts()
    assert len(drafts) == 1
    assert drafts[0].email_id == sample_email.email_id
    assert drafts[0].draft_text == "Sure, lunch works."
    assert db.is_seen(sample_email.email_id)
    assert any("New email" in msg or "Draft reply" in msg for msg, _ in wa.sent_texts)


async def test_check_new_mail_idempotent_one_draft_per_email(patch_clients, sample_email):
    _gmail, wa, llm = patch_clients
    llm.draft_result = DraftResult(status="draft", text="Reply")

    await check_new_mail()
    # simulate re-poll returning same id (already seen) - filter_unseen will skip
    await check_new_mail()

    drafts = db.list_pending_drafts()
    assert len(drafts) == 1
    assert sum(1 for m, _ in wa.sent_texts if "Draft reply" in m) == 1


async def test_check_new_mail_no_reply_needed_marks_seen(patch_clients, sample_email):
    _gmail, wa, llm = patch_clients
    llm.draft_result = DraftResult(status="no_reply")

    created = await check_new_mail()

    assert created == 0
    assert db.list_pending_drafts() == []
    assert db.is_seen(sample_email.email_id)
    assert wa.sent_texts == []


async def test_check_new_mail_suspicious_warns_and_marks_seen(patch_clients, sample_email):
    _gmail, wa, llm = patch_clients
    llm.draft_result = DraftResult(status="suspicious", reason="asks for credentials")

    created = await check_new_mail()

    assert created == 0
    assert db.list_pending_drafts() == []
    assert db.is_seen(sample_email.email_id)
    assert any("Suspicious" in msg for msg, _ in wa.sent_texts)
    assert any("credentials" in msg for msg, _ in wa.sent_texts)


async def test_send_command_sends_and_marks(patch_clients, sample_email):
    gmail, _wa, _llm = patch_clients
    draft_id = db.create_draft(
        email_id=sample_email.email_id,
        thread_id=sample_email.thread_id,
        sender=sample_email.sender,
        subject=sample_email.subject,
        message_id_header=sample_email.message_id,
        draft_text="Draft text",
    )
    gmail.add_email(sample_email)

    reply = await handle_command(f"send {draft_id}")

    assert "Sent reply" in reply
    assert len(gmail.sent) == 1
    sent_email, sent_text = gmail.sent[0]
    assert sent_text == "Draft text"
    assert sent_email.sender == sample_email.sender
    assert sent_email.thread_id == sample_email.thread_id
    assert sent_email.message_id == sample_email.message_id
    draft = db.get_draft(draft_id)
    assert draft.status == "sent"
    assert draft.final_text == "Draft text"


async def test_send_edited_uses_owner_text(patch_clients, sample_email):
    gmail, _wa, _llm = patch_clients
    draft_id = db.create_draft(
        email_id=sample_email.email_id,
        thread_id=sample_email.thread_id,
        sender=sample_email.sender,
        subject=sample_email.subject,
        message_id_header=sample_email.message_id,
        draft_text="Original draft",
    )
    gmail.add_email(sample_email)

    reply = await handle_command(f"send {draft_id} Thanks, Thursday works")

    assert "Sent reply" in reply
    assert gmail.sent[0][1] == "Thanks, Thursday works"
    assert db.get_draft(draft_id).final_text == "Thanks, Thursday works"


async def test_daily_summary_sends(patch_clients, sample_email):
    _gmail, wa, llm = patch_clients
    llm.summary_text = "- Alice asked about lunch"

    summary = await daily_summary()

    assert "Alice asked about lunch" in summary
    assert any("Daily email summary" in msg for msg, _ in wa.sent_texts)
    assert any("Alice asked about lunch" in msg for msg, _ in wa.sent_texts)


async def test_daily_summary_no_emails(patch_clients):
    gmail, wa, _llm = patch_clients
    gmail.set_list_ids([])
    summary = await daily_summary()
    assert summary == ""
    assert any("No new emails" in msg for msg, _ in wa.sent_texts)


async def test_seed_initial_unread_only_once(patch_clients):
    gmail, _wa, _llm = patch_clients
    gmail.set_list_ids(["a", "b", "c"])
    n1 = await seed_initial_unread()
    n2 = await seed_initial_unread()
    assert n1 == 3
    assert n2 == 0
    assert db.get_state(STATE_SEEDED) == "3"
    assert db.is_seen("a") and db.is_seen("b") and db.is_seen("c")
