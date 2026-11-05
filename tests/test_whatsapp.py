"""Tests for WhatsApp client helpers."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.whatsapp import (
    MetaWhatsAppClient,
    WhatsAppError,
    WhatsAppTemplateRequiredError,
    split_message,
)


def test_split_message_short():
    assert split_message("hello", limit=10) == ["hello"]


def test_split_message_prefers_newline():
    text = "line1\nline2\nline3"
    parts = split_message(text, limit=7)
    assert parts == ["line1", "line2", "line3"]
    assert all(len(p) <= 7 for p in parts)


def test_split_message_hard_cut():
    assert split_message("abcdefgh", limit=3) == ["abc", "def", "gh"]


@respx.mock
async def test_meta_send_text(monkeypatch):
    monkeypatch.setenv("WHATSAPP_TOKEN", "tok")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "999")
    monkeypatch.setenv("WHATSAPP_API_BASE_URL", "https://graph.test/v1")
    from app.config import get_settings

    get_settings.cache_clear()
    route = respx.post("https://graph.test/v1/999/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
    )
    client = MetaWhatsAppClient(token="tok", phone_id="999", base_url="https://graph.test/v1")
    await client.send_text("hello", to="923001234567")
    assert route.called
    body = route.calls[0].request.content
    assert b"hello" in body
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    await client.aclose()


@respx.mock
async def test_meta_send_text_splits_long():
    route = respx.post("https://graph.test/v1/999/messages").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = MetaWhatsAppClient(token="tok", phone_id="999", base_url="https://graph.test/v1")
    await client.send_text("a" * 9000, to="1")
    assert route.call_count == 3  # 4000 + 4000 + 1000
    await client.aclose()


@respx.mock
async def test_meta_template_required_error():
    respx.post("https://graph.test/v1/999/messages").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "Re-engagement message", "code": 131047}},
        )
    )
    client = MetaWhatsAppClient(token="tok", phone_id="999", base_url="https://graph.test/v1")
    with pytest.raises(WhatsAppTemplateRequiredError):
        await client.send_text("hi", to="1")
    await client.aclose()


@respx.mock
async def test_meta_other_error():
    respx.post("https://graph.test/v1/999/messages").mock(
        return_value=httpx.Response(500, json={"error": {"message": "boom", "code": 0}})
    )
    client = MetaWhatsAppClient(token="tok", phone_id="999", base_url="https://graph.test/v1")
    with pytest.raises(WhatsAppError):
        await client.send_text("hi", to="1")
    await client.aclose()
