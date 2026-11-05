"""Tests for Gmail helpers (body extraction, subjects, reply construction)."""

from __future__ import annotations

import base64

from app.gmail_client import Email, _reply_subject, extract_body, html_to_text


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_html_to_text_basic():
    html = "<p>Hello<br>world</p><div>Bye</div>"
    text = html_to_text(html)
    assert "Hello" in text
    assert "world" in text
    assert "Bye" in text
    assert "<p>" not in text


def test_extract_body_prefers_plain():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("plain body")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<b>html</b>")}},
        ],
    }
    assert extract_body(payload) == "plain body"


def test_extract_body_falls_back_to_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/html", "body": {"data": _b64url("<p>html body</p>")}}],
    }
    assert "html body" in extract_body(payload)


def test_extract_body_truncates():
    payload = {"mimeType": "text/plain", "body": {"data": _b64url("x" * 100)}}
    out = extract_body(payload, max_chars=20)
    assert out.endswith("…[truncated]")
    assert len(out) <= 20 + len("\n…[truncated]")


def test_reply_subject_prefix_once():
    assert _reply_subject("Hello") == "Re: Hello"
    assert _reply_subject("Re: Hello") == "Re: Hello"
    assert _reply_subject("RE: Hello") == "RE: Hello"
    assert _reply_subject("") == "Re: (no subject)"


def test_email_preview():
    e = Email("1", "t", "a@b.com", "s", "<m>", "", "hello\nworld   test")
    assert e.preview() == "hello world test"
    assert e.preview(5).endswith("…")
