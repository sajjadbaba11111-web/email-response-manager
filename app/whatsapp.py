"""WhatsApp delivery.

``WhatsAppClient`` is a small protocol so that the Meta Cloud API
implementation can be swapped for a Twilio (or any other) provider later.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

#: Meta error codes that mean "free-form message is no longer allowed".
_TEMPLATE_REQUIRED_CODES: frozenset[int] = frozenset({131047, 131056, 133016})
_TEMPLATE_REQUIRED_MARKERS: tuple[str, ...] = (
    "24 hour",
    "24-hour",
    "template",
)


class WhatsAppError(RuntimeError):
    """Raised when a WhatsApp message could not be delivered."""

    def __init__(self, message: str, *, code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class WhatsAppTemplateRequiredError(WhatsAppError):
    """Raised when the 24 hour customer-service window has closed.

    Meta only allows free-form messages inside the 24 hours after the
    recipient's last message.  Outside that window an approved template has
    to be used instead.
    """


@runtime_checkable
class WhatsAppClient(Protocol):
    """Transport agnostic WhatsApp sender."""

    async def send_text(self, text: str, to: str | None = None) -> None:
        """Send a free-form text message."""

    async def send_template(
        self,
        to: str | None,
        template_name: str,
        language_code: str,
        parameters: list[str] | None = None,
    ) -> None:
        """Send a pre-approved template message."""


def split_message(text: str, limit: int = 4000) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters.

    Chunks are cut on line boundaries when possible so that WhatsApp does not
    render half a paragraph.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class MetaWhatsAppClient:
    """WhatsApp Cloud API (Meta) implementation of :class:`WhatsAppClient`."""

    def __init__(
        self,
        *,
        token: str | None = None,
        phone_id: str | None = None,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        settings = get_settings()
        self._token = token or settings.whatsapp_token
        self._phone_id = phone_id or settings.whatsapp_phone_id
        self._base_url = (base_url or settings.whatsapp_api_base_url).rstrip("/")
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    # -- plumbing ------------------------------------------------------------
    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""

        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    def _endpoint(self) -> str:
        return f"{self._base_url}/{self._phone_id}/messages"

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        code: int | None = None
        message = response.text
        try:
            payload: Any = response.json()
            error = (payload or {}).get("error") or {}
            code = error.get("code")
            message = error.get("message") or message
            details = error.get("error_subcode")
            if details is not None:
                message = f"{message} (subcode {details})"
        except ValueError:
            pass

        lowered = (message or "").lower()
        needs_template = code in _TEMPLATE_REQUIRED_CODES or any(
            marker in lowered for marker in _TEMPLATE_REQUIRED_MARKERS
        )
        logger.error(
            "WhatsApp API rejected the message",
            extra={"status_code": response.status_code, "error_code": code},
        )
        if needs_template:
            raise WhatsAppTemplateRequiredError(
                "WhatsApp rejected the message because the 24 hour window is closed "
                f"(code {code}): {message}",
                code=code,
                status_code=response.status_code,
            )
        raise WhatsAppError(
            f"WhatsApp API error {response.status_code} (code {code}): {message}",
            code=code,
            status_code=response.status_code,
        )

    # -- public API ----------------------------------------------------------
    async def send_text(self, text: str, to: str | None = None) -> None:
        """Send ``text`` to ``to`` (defaults to the configured number)."""

        settings = get_settings()
        recipient = to or settings.my_whatsapp_number
        client = await self._http()
        chunks = split_message(text)
        for chunk in chunks:
            body = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"preview_url": False, "body": chunk},
            }
            response = await client.post(
                self._endpoint(),
                json=body,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._raise_for_error(response)
        logger.info("WhatsApp text sent", extra={"recipient": recipient, "chunks": len(chunks)})

    async def send_template(
        self,
        to: str | None,
        template_name: str,
        language_code: str,
        parameters: list[str] | None = None,
    ) -> None:
        """Send a pre-approved template message."""

        settings = get_settings()
        recipient = to or settings.my_whatsapp_number
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        }
        if parameters:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": str(value)} for value in parameters],
                }
            ]

        client = await self._http()
        response = await client.post(
            self._endpoint(),
            json=payload,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        self._raise_for_error(response)
        logger.info(
            "WhatsApp template sent",
            extra={"recipient": recipient, "template": template_name},
        )


_client: WhatsAppClient | None = None


def get_whatsapp_client() -> WhatsAppClient:
    """Return the process wide WhatsApp client."""

    global _client
    if _client is None:
        _client = MetaWhatsAppClient()
    return _client


def reset_whatsapp_client() -> None:
    """Drop the cached client (used by tests)."""

    global _client
    _client = None
