"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

_RESERVED = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
}


class SecretRedactingFilter(logging.Filter):
    """Replace configured secret strings with ``[REDACTED]`` in log records."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        for attr in ("msg",):
            value = getattr(record, attr, None)
            if isinstance(value, str):
                for secret in self._secrets:
                    value = value.replace(secret, "[REDACTED]")
                setattr(record, attr, value)
        if record.args:
            record.args = self._scrub(record.args)
        return True

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, tuple):
            return tuple(self._scrub(v) for v in value)
        if isinstance(value, dict):
            return {k: self._scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        return value


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    """Configure root logging with JSON output and secret redaction."""

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(SecretRedactingFilter(secrets))
    root.addHandler(handler)

    # Keep third-party libraries from flooding the log.
    for name in ("httpx", "httpcore", "googleapiclient.discovery_cache"):
        logging.getLogger(name).setLevel(logging.WARNING)
