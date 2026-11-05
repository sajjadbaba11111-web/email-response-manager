"""Tests for the WhatsApp webhook route."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.config import get_settings
from app.routes_whatsapp import WEBHOOK_PATH, extract_messages, router, verify_signature


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_verify_signature_valid():
    body = b'{"a":1}'
    assert verify_signature(body, _sign(body, "secret"), "secret") is True


def test_verify_signature_invalid():
    assert verify_signature(b"{}", "sha256=deadbeef", "secret") is False
    assert verify_signature(b"{}", None, "secret") is False


def test_extract_messages_text_only():
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.1",
                                    "from": "923001234567",
                                    "type": "text",
                                    "text": {"body": "hello"},
                                },
                                {"id": "wamid.2", "from": "923001234567", "type": "image"},
                            ]
                        }
                    }
                ]
            }
        ]
    }
    msgs = extract_messages(payload)
    assert len(msgs) == 1
    assert msgs[0].text == "hello"
    assert msgs[0].message_id == "wamid.1"


def test_webhook_verify_success():
    client = TestClient(_app())
    token = get_settings().whatsapp_verify_token
    r = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.verify_token": token, "hub.challenge": "abc"},
    )
    assert r.status_code == 200
    assert r.text == "abc"


def test_webhook_verify_failure():
    client = TestClient(_app())
    r = client.get(
        WEBHOOK_PATH,
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "abc"},
    )
    assert r.status_code == 403


def test_webhook_rejects_bad_signature():
    client = TestClient(_app())
    body = json.dumps({"entry": []}).encode()
    r = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={"X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"},
    )
    assert r.status_code == 403


def test_webhook_rejects_missing_signature():
    client = TestClient(_app())
    r = client.post(WEBHOOK_PATH, json={"entry": []})
    assert r.status_code == 403


def test_webhook_ignores_unknown_sender(monkeypatch):
    import app.routes_whatsapp as rw

    monkeypatch.setattr(rw, "process_inbound_message", AsyncMock())
    client = TestClient(_app())
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.x",
                                    "from": "10000000000",
                                    "type": "text",
                                    "text": {"body": "send 1"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    secret = get_settings().whatsapp_app_secret
    r = client.post(
        WEBHOOK_PATH,
        content=body,
        headers={"X-Hub-Signature-256": _sign(body, secret), "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] == "0"
    assert db.mark_wa_message_processed("wamid.x") is True  # never claimed


def test_webhook_dedupes_message_id(monkeypatch):
    import app.routes_whatsapp as rw

    # Run background tasks synchronously in effect? TestClient runs them; we patch to no-op.
    calls = []

    async def fake_process(msg):
        calls.append(msg.message_id)

    monkeypatch.setattr(rw, "process_inbound_message", fake_process)
    client = TestClient(_app())
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "id": "wamid.dup",
                                    "from": "923001234567",
                                    "type": "text",
                                    "text": {"body": "help"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode()
    secret = get_settings().whatsapp_app_secret
    headers = {"X-Hub-Signature-256": _sign(body, secret), "Content-Type": "application/json"}
    r1 = client.post(WEBHOOK_PATH, content=body, headers=headers)
    r2 = client.post(WEBHOOK_PATH, content=body, headers=headers)
    assert r1.json()["accepted"] == "1"
    assert r2.json()["accepted"] == "0"
    assert calls.count("wamid.dup") == 1
