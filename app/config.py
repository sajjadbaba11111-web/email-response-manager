"""Environment driven configuration.

Every setting is read from the process environment (optionally seeded from a
``.env`` file at the project root).  Missing required values raise a
:class:`ConfigurationError` with an actionable message instead of a raw
pydantic traceback.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

#: Scopes requested during the one-time Google OAuth flow.
GMAIL_SCOPES: Final[tuple[str, ...]] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)


class ConfigurationError(RuntimeError):
    """Raised when the process environment is not configured correctly."""


def normalize_number(value: str) -> str:
    """Return only the digits of a phone number (no ``+``, spaces or dashes)."""

    return re.sub(r"\D", "", value or "")


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic -------------------------------------------------------
    anthropic_api_key: str = Field(..., description="Anthropic API key used by the Claude wrapper.")
    claude_model: str = Field(default="claude-sonnet-5-5", description="Claude model id to call.")

    # --- WhatsApp Cloud API ----------------------------------------------
    whatsapp_token: str = Field(..., description="Meta WhatsApp Cloud API access token.")
    whatsapp_phone_id: str = Field(..., description="WhatsApp Cloud API sender phone-number id.")
    whatsapp_verify_token: str = Field(
        ..., description="Token echoed back to Meta during webhook verification."
    )
    whatsapp_app_secret: str = Field(
        ..., description="Meta app secret used to validate the X-Hub-Signature-256 header."
    )
    my_whatsapp_number: str = Field(
        ..., description="Your own WhatsApp number, digits only (e.g. 923001234567)."
    )
    whatsapp_api_base_url: str = Field(
        default="https://graph.facebook.com/v21.0", description="Meta Graph API base url."
    )
    whatsapp_template_name: str = Field(
        default="daily_summary_opener",
        description="Approved template used when a free-form message is outside the 24h window.",
    )
    whatsapp_template_language: str = Field(default="en", description="Template language code.")

    # --- Gmail -------------------------------------------------------------
    gmail_token_path: Path = Field(
        default=PROJECT_ROOT / "token.json", description="Path to the OAuth token file."
    )
    gmail_max_results: int = Field(default=50, ge=1, le=500, description="Max emails per query.")

    # --- Scheduling --------------------------------------------------------
    summary_hour: int = Field(default=8, ge=0, le=23, description="Hour of the daily summary.")
    summary_minute: int = Field(default=0, ge=0, le=59, description="Minute of the daily summary.")
    summary_timezone: str = Field(
        default="Asia/Karachi", description="IANA timezone of the summary."
    )
    poll_interval_minutes: int = Field(default=2, ge=1, description="Gmail polling interval.")

    # --- Storage -------------------------------------------------------------
    db_path: Path = Field(
        default=PROJECT_ROOT / "data" / "agent.db",
        description="SQLite file path used when DATABASE_URL is not set.",
    )
    database_url: str = Field(
        default="",
        description=(
            "PostgreSQL connection URL (e.g. postgresql://user:pass@host:5432/db). "
            "When set, PostgreSQL is used instead of SQLite."
        ),
    )

    # --- MCP ------------------------------------------------------------------
    mcp_api_key: str = Field(..., description="Bearer token required by the /mcp endpoint.")

    # --- Admin UI -------------------------------------------------------------
    admin_username: str = Field(default="admin", description="Admin dashboard username.")
    admin_password: str = Field(
        default="",
        description="Admin dashboard password. Leave empty to disable the admin UI.",
    )
    admin_secret_key: str = Field(
        default="",
        description="Secret key for signing admin session cookies.",
    )

    # --- Tuning -----------------------------------------------------------------
    log_level: str = Field(default="INFO", description="Root log level.")
    llm_max_tokens: int = Field(
        default=1024, ge=64, description="Max output tokens per Claude call."
    )
    llm_timeout_seconds: float = Field(default=60.0, gt=0, description="Claude request timeout.")
    llm_max_retries: int = Field(default=3, ge=0, description="Claude retry attempts.")
    max_email_body_chars: int = Field(
        default=6000, ge=500, description="Truncation limit for a single email body."
    )
    error_notice_cooldown_minutes: int = Field(
        default=30,
        ge=0,
        description="Minimum minutes between two identical WhatsApp error notices.",
    )

    @field_validator("my_whatsapp_number")
    @classmethod
    def _normalize_phone(cls, value: str) -> str:
        digits = normalize_number(value)
        if not digits:
            raise ValueError("MY_WHATSAPP_NUMBER must contain at least one digit")
        return digits

    @field_validator("summary_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("gmail_token_path", "db_path")
    @classmethod
    def _resolve_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value)

    @property
    def summary_tz(self) -> ZoneInfo:
        """Timezone object used by the scheduler and the summary rendering."""

        return ZoneInfo(self.summary_timezone)

    @property
    def use_postgres(self) -> bool:
        return bool((self.database_url or "").strip())

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_password and self.admin_secret_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached settings, failing fast with a readable message."""

    try:
        return Settings()
    except ValidationError as exc:
        missing = sorted(
            str(error["loc"][0]).upper() for error in exc.errors() if error["type"] == "missing"
        )
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill in the values."
            ) from exc
        raise ConfigurationError(f"Invalid configuration: {exc}") from exc
