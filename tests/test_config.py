"""Tests for configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings, get_settings, normalize_number


def test_normalize_number():
    assert normalize_number("+92 300-1234567") == "923001234567"
    assert normalize_number("923001234567") == "923001234567"


def test_settings_loads_required(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("WHATSAPP_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "p")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s-secret-value")
    monkeypatch.setenv("MY_WHATSAPP_NUMBER", "+92 300 1234567")
    monkeypatch.setenv("MCP_API_KEY", "mcp-key-long")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "a.db"))
    get_settings.cache_clear()
    s = Settings()
    assert s.my_whatsapp_number == "923001234567"
    assert s.summary_timezone == "Asia/Karachi"


def test_missing_required_raises(monkeypatch, tmp_path):
    for k in list(__import__("os").environ):
        if k in {
            "ANTHROPIC_API_KEY",
            "WHATSAPP_TOKEN",
            "WHATSAPP_PHONE_ID",
            "WHATSAPP_VERIFY_TOKEN",
            "WHATSAPP_APP_SECRET",
            "MY_WHATSAPP_NUMBER",
            "MCP_API_KEY",
        }:
            monkeypatch.delenv(k, raising=False)
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        get_settings()


def test_invalid_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("WHATSAPP_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_ID", "p")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s-secret-value")
    monkeypatch.setenv("MY_WHATSAPP_NUMBER", "923001234567")
    monkeypatch.setenv("MCP_API_KEY", "mcp-key-long")
    monkeypatch.setenv("SUMMARY_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings()
