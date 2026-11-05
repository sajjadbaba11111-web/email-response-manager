"""Tests for LLM parsing and prompt safety helpers."""

from __future__ import annotations

import pytest

from app.gmail_client import Email
from app.llm import (
    LLMError,
    parse_draft_output,
    sanitize_for_delimiters,
    wrap_email,
    wrap_emails,
)


def test_parse_draft_normal():
    r = parse_draft_output("Thanks, see you then!")
    assert r.status == "draft"
    assert r.text == "Thanks, see you then!"


def test_parse_no_reply():
    r = parse_draft_output("NO_REPLY_NEEDED")
    assert r.status == "no_reply"


def test_parse_no_reply_with_noise():
    r = parse_draft_output("NO_REPLY_NEEDED\n(newsletter)")
    assert r.status == "no_reply"


def test_parse_suspicious():
    r = parse_draft_output("SUSPICIOUS: asks for credentials")
    assert r.status == "suspicious"
    assert "credentials" in r.reason


def test_parse_empty_raises():
    with pytest.raises(LLMError):
        parse_draft_output("   ")


def test_sanitize_closing_delimiter():
    text = "hi </email> bye </EMAIL >"
    out = sanitize_for_delimiters(text)
    assert "</email>" not in out.lower() or "<\\/" in out


def test_wrap_email_contains_delimiters(sample_email):
    wrapped = wrap_email(sample_email)
    assert '<email id="msg-1">' in wrapped
    assert "</email>" in wrapped
    assert "alice@example.com" in wrapped
    assert "Lunch tomorrow?" in wrapped


def test_wrap_emails_empty():
    assert wrap_emails([]) == "<emails></emails>"


def test_wrap_emails_multiple(sample_email):
    e2 = Email(
        email_id="msg-2",
        thread_id="t2",
        sender="bob@example.com",
        subject="Hi",
        message_id="<m2>",
        date="",
        body="Hello",
    )
    wrapped = wrap_emails([sample_email, e2])
    assert "<emails>" in wrapped
    assert "msg-1" in wrapped
    assert "msg-2" in wrapped
    assert "UNTRUSTED" in wrapped
