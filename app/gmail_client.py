"""Async wrapper around the Gmail REST API.

The Google client library is blocking, so every call is executed in a worker
thread via :func:`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Final

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import GMAIL_SCOPES, get_settings

logger = logging.getLogger(__name__)

_SCRIPT_STYLE_RE: Final[re.Pattern[str]] = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
_BR_RE: Final[re.Pattern[str]] = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)
_BLOCK_END_RE: Final[re.Pattern[str]] = re.compile(
    r"</\s*(p|div|tr|li|h[1-6]|blockquote|section)\s*>", re.IGNORECASE
)
_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]*>")
_MULTI_NEWLINE_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")
_SENDER_RE: Final[re.Pattern[str]] = re.compile(r"<([^>]+)>")


class GmailError(RuntimeError):
    """Raised when a Gmail operation fails."""


class GmailAuthError(GmailError):
    """Raised when the OAuth credentials are missing, invalid or out of scope."""


@dataclass(slots=True)
class Email:
    """A normalised Gmail message."""

    email_id: str
    thread_id: str
    sender: str
    subject: str
    message_id: str
    date: str
    body: str

    def preview(self, limit: int = 200) -> str:
        """Return a short single-line preview of the body."""

        collapsed = " ".join(self.body.split())
        if len(collapsed) <= limit:
            return collapsed
        return collapsed[:limit].rstrip() + "…"


def _decode_b64(data: str) -> str:
    """Decode base64url payload data into text."""

    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except Exception:  # pragma: no cover - malformed payload
        return ""
    return raw.decode("utf-8", errors="replace")


def html_to_text(html_body: str) -> str:
    """Convert an HTML body into readable plain text."""

    text = _SCRIPT_STYLE_RE.sub(" ", html_body)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = "\n".join(" ".join(line.split()) for line in text.splitlines())
    text = _MULTI_NEWLINE_RE.sub("\n\n", text)
    return text.strip()


def _collect_bodies(part: dict[str, Any], plain: list[str], html_parts: list[str]) -> None:
    """Walk a Gmail payload tree collecting text/plain and text/html parts."""

    mime_type = str(part.get("mimeType", ""))
    body = part.get("body") or {}
    data = body.get("data")
    if data and mime_type == "text/plain":
        plain.append(_decode_b64(data))
    elif data and mime_type == "text/html":
        html_parts.append(_decode_b64(data))

    for child in part.get("parts") or []:
        if isinstance(child, dict):
            _collect_bodies(child, plain, html_parts)


def extract_body(payload: dict[str, Any], max_chars: int = 6000) -> str:
    """Return the plain-text body of a Gmail payload."""

    plain: list[str] = []
    html_parts: list[str] = []
    _collect_bodies(payload or {}, plain, html_parts)

    text = "\n".join(part for part in plain if part.strip()).strip()
    if not text and html_parts:
        text = html_to_text("\n".join(html_parts))

    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n…[truncated]"
    return text


def _header(headers: list[dict[str, Any]], name: str) -> str:
    wanted = name.lower()
    for header in headers or []:
        if str(header.get("name", "")).lower() == wanted:
            return str(header.get("value", ""))
    return ""


def _reply_subject(subject: str) -> str:
    """Prefix ``Re:`` exactly once."""

    cleaned = (subject or "").strip() or "(no subject)"
    if cleaned.lower().startswith("re:"):
        return cleaned
    return f"Re: {cleaned}"


class GmailClient:
    """Small async facade over the Gmail API."""

    def __init__(
        self,
        token_path: Path | None = None,
        *,
        max_body_chars: int | None = None,
    ) -> None:
        settings = get_settings()
        self._token_path = Path(token_path) if token_path else settings.gmail_token_path
        self._max_body_chars = max_body_chars or settings.max_email_body_chars
        self._service: Any | None = None
        self._lock = asyncio.Lock()

    # -- credentials -------------------------------------------------------
    def _load_credentials(self) -> Credentials:
        path = self._token_path
        if not path.exists():
            raise GmailAuthError(
                f"Gmail token file {path} does not exist. Run 'python scripts/gmail_auth.py' first."
            )
        try:
            creds = Credentials.from_authorized_user_file(str(path), list(GMAIL_SCOPES))
        except Exception as exc:
            raise GmailAuthError(f"Could not read Gmail token file {path}: {exc}") from exc

        if creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Gmail access token")
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise GmailAuthError(f"Could not refresh the Gmail access token: {exc}") from exc
            try:
                path.write_text(creds.to_json(), encoding="utf-8")
            except OSError as exc:  # pragma: no cover - defensive
                logger.warning("Could not persist the refreshed Gmail token: %s", exc)
        elif not creds.valid:
            raise GmailAuthError(
                "The Gmail access token is invalid or expired and cannot be refreshed. "
                "Re-run 'python scripts/gmail_auth.py'."
            )

        missing = [scope for scope in GMAIL_SCOPES if scope not in (creds.scopes or [])]
        if missing:
            raise GmailAuthError(
                "The Gmail token is missing required scope(s): "
                + ", ".join(missing)
                + ". Re-run 'python scripts/gmail_auth.py' and grant access to your own account."
            )
        return creds

    def _get_service(self) -> Any:
        if self._service is None:
            creds = self._load_credentials()
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _reset_service(self) -> None:
        self._service = None

    # -- read ---------------------------------------------------------------
    def _list_message_ids_sync(self, query: str, limit: int) -> list[str]:
        service = self._get_service()
        response = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=max(1, min(limit, 500)))
            .execute()
        )
        return [str(item["id"]) for item in (response.get("messages") or [])]

    async def list_message_ids(self, query: str, limit: int = 50) -> list[str]:
        """Return the ids of the messages matching a Gmail search query."""

        try:
            return await asyncio.to_thread(self._list_message_ids_sync, query, limit)
        except GmailError:
            raise
        except HttpError as exc:
            raise GmailError(f"Gmail query failed: {exc}") from exc

    def _get_email_sync(self, email_id: str) -> Email:
        service = self._get_service()
        message = service.users().messages().get(userId="me", id=email_id, format="full").execute()
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []

        raw_from = _header(headers, "From")
        match = _SENDER_RE.search(raw_from)
        sender = (match.group(1) if match else raw_from).strip()

        return Email(
            email_id=str(message.get("id", email_id)),
            thread_id=str(message.get("threadId", "")),
            sender=sender,
            subject=_header(headers, "Subject"),
            message_id=_header(headers, "Message-ID"),
            date=_header(headers, "Date"),
            body=extract_body(payload, self._max_body_chars),
        )

    async def get_email(self, email_id: str) -> Email:
        """Fetch and normalise a single email."""

        try:
            return await asyncio.to_thread(self._get_email_sync, email_id)
        except GmailError:
            raise
        except HttpError as exc:
            raise GmailError(f"Could not fetch email {email_id}: {exc}") from exc

    # -- write ---------------------------------------------------------------
    def _send_reply_sync(self, email: Email, text: str) -> str:
        message = EmailMessage()
        message["To"] = email.sender
        message["Subject"] = _reply_subject(email.subject)
        if email.message_id:
            message["In-Reply-To"] = email.message_id
            message["References"] = email.message_id
        message["Message-ID"] = make_msgid(domain="gmail.com")
        message.set_content(text)

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        body: dict[str, Any] = {"raw": raw}
        if email.thread_id:
            body["threadId"] = email.thread_id

        service = self._get_service()
        result = service.users().messages().send(userId="me", body=body).execute()
        return str(result.get("id", ""))

    async def send_reply(self, email: Email, text: str) -> str:
        """Send ``text`` as a reply to ``email`` inside the original thread.

        The reply only ever goes to the original sender: no CC, no BCC.
        """

        try:
            return await asyncio.to_thread(self._send_reply_sync, email, text)
        except GmailError:
            raise
        except HttpError as exc:
            raise GmailError(f"Could not send the reply: {exc}") from exc

    # -- diagnostics ---------------------------------------------------------
    def token_status(self) -> dict[str, Any]:
        """Return a redacted description of the stored OAuth token."""

        try:
            creds = self._load_credentials()
        except GmailError as exc:
            return {"valid": False, "detail": str(exc)}
        return {
            "valid": bool(creds.valid),
            "expired": bool(creds.expired),
            "has_refresh_token": bool(creds.refresh_token),
            "scopes": sorted(creds.scopes or []),
        }


_client: GmailClient | None = None


def get_gmail_client() -> GmailClient:
    """Return the process wide Gmail client."""

    global _client
    if _client is None:
        _client = GmailClient()
    return _client


def reset_gmail_client() -> None:
    """Drop the cached client (used by tests and after a re-auth)."""

    global _client
    _client = None
