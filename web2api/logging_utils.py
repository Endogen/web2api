"""Structured logging helpers shared across the application."""

from __future__ import annotations

import contextvars
import logging
from typing import Any
from uuid import uuid4

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "web2api_request_id",
    default="-",
)


def build_request_id(header_value: str | None) -> str:
    """Return a normalized request id from an incoming header value."""
    candidate = (header_value or "").strip()
    if not candidate:
        return uuid4().hex
    return candidate[:128]


def set_request_id(request_id: str) -> contextvars.Token[str]:
    """Store the request id in request-local context."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: contextvars.Token[str]) -> None:
    """Reset request-local context to the previous request id."""
    _REQUEST_ID.reset(token)


def get_request_id() -> str:
    """Return the current request id from context."""
    return _REQUEST_ID.get()


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    exc_info: Any | None = None,
    **fields: Any,
) -> None:
    """Emit a structured log event with request context fields."""
    payload: dict[str, Any] = {
        "event": event,
        "request_id": get_request_id(),
    }
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.log(level, event, extra=payload, exc_info=exc_info)
