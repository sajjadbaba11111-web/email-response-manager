"""Shared pytest fixtures for the email ↔ WhatsApp agent tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Ensure required env vars exist before app.config is imported by tests.
_DEFAULT_ENV = {
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "CLAUDE_MODEL": "claude-sonnet-test",
    "WHATSAPP_TOKEN": "test-wa-token",
    "WHATSAPP_PHONE_ID": "1234567890",
    "WHATSAPP_VERIFY_TOKEN": "test-verify-token",
    "WHATSAPP_APP_SECRET": "test-app-secret-value",
    "MY_WHATSAPP_NUMBER": "923001234567",
    "GMAIL_TOKEN_PATH": "token.json",
    "SUMMARY_HOUR": "8",
    "SUMMARY_MINUTE": "0",
    "SUMMARY_TIMEZONE": "Asia/Karachi",
    "POLL_INTERVAL_MINUTES": "2",
    "DB_PATH": "data/test.db",
    "MCP_API_KEY": "test-mcp-api-key",
}
for _k, _v in _DEFAULT_ENV.items():
    os.environ.setdefault(_k, _v)

from app import db  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.gmail_client import Email, reset_gmail_client  # noqa: E402
from app.llm import reset_llm  # noqa: E402
from app.services import reset_error_notices  # noqa: E402
from app.whatsapp import reset_whatsapp_client  # noqa: E402


def _clear_caches() -> None:
    get_settings.cache_clear()
    reset_gmail_client()
    reset_whatsapp_client()
    reset_llm()
    reset_error_notices()


@pytest.fixture(autouse=True)
def env_and_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point settings at a fresh temp DB and reset all singletons."""
    db_path = tmp_path / "agent.db"
    token_path = tmp_path / "token.json"
    for key, value in _DEFAULT_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
    _clear_caches()
    db.init_db()
    yield db_path
    _clear_caches()


@pytest.fixture
def sample_email() -> Email:
    return Email(
        email_id="msg-1",
        thread_id="thread-1",
        sender="alice@example.com",
        subject="Lunch tomorrow?",
        message_id="<msg1@example.com>",
        date="Mon, 1 Jan 2026 10:00:00 +0000",
        body="Are we still on for lunch tomorrow?",
    )


class FakeGmailClient:
    def __init__(self, emails: list[Email] | None = None) -> None:
        self._emails = {e.email_id: e for e in (emails or [])}
        self.sent: list[tuple[Email, str]] = []
        self._list_ids: list[str] = list(self._emails)

    def set_list_ids(self, ids: list[str]) -> None:
        self._list_ids = ids

    def add_email(self, email: Email) -> None:
        self._emails[email.email_id] = email
        if email.email_id not in self._list_ids:
            self._list_ids.append(email.email_id)

    async def list_message_ids(self, query: str, limit: int = 50) -> list[str]:
        return list(self._list_ids)[:limit]

    async def get_email(self, email_id: str) -> Email:
        if email_id not in self._emails:
            raise KeyError(email_id)
        return self._emails[email_id]

    async def send_reply(self, email: Email, text: str) -> str:
        self.sent.append((email, text))
        return "sent-gmail-id"

    def token_status(self) -> dict[str, Any]:
        return {"valid": True, "expired": False, "has_refresh_token": True, "scopes": []}


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.sent_texts: list[tuple[str, str | None]] = []
        self.sent_templates: list[tuple[str | None, str, str, list[str] | None]] = []

    async def send_text(self, text: str, to: str | None = None) -> None:
        self.sent_texts.append((text, to))

    async def send_template(
        self,
        to: str | None,
        template_name: str,
        language_code: str,
        parameters: list[str] | None = None,
    ) -> None:
        self.sent_templates.append((to, template_name, language_code, parameters))

    async def aclose(self) -> None:
        return None


class FakeLLM:
    def __init__(self) -> None:
        self.draft_result: Any = None
        self.summary_text = "Summary"
        self.draft_calls: list[Email] = []
        self.summarize_calls: list[list[Email]] = []

    async def draft_reply(self, email: Email):
        from app.llm import DraftResult

        self.draft_calls.append(email)
        if self.draft_result is not None:
            return self.draft_result
        return DraftResult(status="draft", text="Sure, that works for me.")

    async def summarize(self, emails: list[Email]) -> str:
        self.summarize_calls.append(list(emails))
        return self.summary_text

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_gmail(sample_email: Email) -> FakeGmailClient:
    return FakeGmailClient([sample_email])


@pytest.fixture
def fake_whatsapp() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def patch_clients(
    monkeypatch: pytest.MonkeyPatch,
    fake_gmail: FakeGmailClient,
    fake_whatsapp: FakeWhatsAppClient,
    fake_llm: FakeLLM,
):
    """Patch the singleton getters to return fakes."""
    import app.gmail_client
    import app.llm
    import app.mcp_server
    import app.services
    import app.whatsapp

    monkeypatch.setattr(app.gmail_client, "get_gmail_client", lambda: fake_gmail)
    monkeypatch.setattr(app.services, "get_gmail_client", lambda: fake_gmail)
    monkeypatch.setattr(app.mcp_server, "get_gmail_client", lambda: fake_gmail)
    monkeypatch.setattr(app.whatsapp, "get_whatsapp_client", lambda: fake_whatsapp)
    monkeypatch.setattr(app.services, "get_whatsapp_client", lambda: fake_whatsapp)
    monkeypatch.setattr(app.mcp_server, "get_whatsapp_client", lambda: fake_whatsapp)
    monkeypatch.setattr(app.llm, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(app.services, "get_llm", lambda: fake_llm)
    monkeypatch.setattr(app.mcp_server, "get_llm", lambda: fake_llm)
    return fake_gmail, fake_whatsapp, fake_llm
