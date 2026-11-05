"""Tests for MCP tools and bearer-token protection."""

from __future__ import annotations

from fastapi import FastAPI

from app import db
from app.config import get_settings
from app.llm import DraftResult
from app.main import MCP_PATH, MCPBearerAuthMiddleware
from app.mcp_server import draft_reply, list_emails, list_pending_drafts, read_email, send_whatsapp


async def test_mcp_tool_list_emails(patch_clients, sample_email):
    _gmail, _wa, _llm = patch_clients
    items = await list_emails(query="is:unread", limit=10)
    assert len(items) == 1
    assert items[0]["email_id"] == sample_email.email_id
    assert items[0]["sender"] == sample_email.sender


async def test_mcp_tool_read_email(patch_clients, sample_email):
    _gmail, _wa, _llm = patch_clients
    item = await read_email(sample_email.email_id)
    assert item["subject"] == sample_email.subject
    assert "lunch" in item["body"].lower()


async def test_mcp_tool_draft_reply_creates_pending(patch_clients, sample_email):
    _gmail, _wa, llm = patch_clients
    llm.draft_result = DraftResult(status="draft", text="See you there")
    result = await draft_reply(sample_email.email_id)
    assert result["status"] == "pending"
    assert isinstance(result["draft_id"], int)
    assert db.get_draft(result["draft_id"]).draft_text == "See you there"


async def test_mcp_tool_draft_reply_no_reply(patch_clients, sample_email):
    _gmail, _wa, llm = patch_clients
    llm.draft_result = DraftResult(status="no_reply")
    result = await draft_reply(sample_email.email_id)
    assert result["status"] == "no_reply"
    assert result["draft_id"] is None


async def test_mcp_tool_list_pending_drafts():
    db.create_draft(
        email_id="e1",
        thread_id="t1",
        sender="a@example.com",
        subject="S",
        message_id_header="<m>",
        draft_text="D",
    )
    items = await list_pending_drafts()
    assert len(items) == 1
    assert items[0]["sender"] == "a@example.com"


async def test_mcp_tool_send_whatsapp(patch_clients):
    _gmail, wa, _llm = patch_clients
    result = await send_whatsapp("hi owner")
    assert result["sent"] is True
    assert wa.sent_texts[-1][0] == "hi owner"


def _dummy_mcp_app():
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        body = b'{"ok":true}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return app


async def test_mcp_middleware_rejects_missing_bearer():
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.add_middleware(MCPBearerAuthMiddleware, mcp_app=_dummy_mcp_app())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(MCP_PATH, json={})
    assert r.status_code == 401
    assert "unauthorized" in r.text.lower()


async def test_mcp_middleware_rejects_wrong_bearer():
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.add_middleware(MCPBearerAuthMiddleware, mcp_app=_dummy_mcp_app())

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(MCP_PATH, json={}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


async def test_mcp_middleware_accepts_correct_bearer():
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.add_middleware(MCPBearerAuthMiddleware, mcp_app=_dummy_mcp_app())
    key = get_settings().mcp_api_key

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(MCP_PATH, json={}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
