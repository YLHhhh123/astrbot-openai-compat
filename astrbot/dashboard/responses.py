from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.core import logger
from astrbot.core.utils.error_redaction import redact_sensitive_text


@dataclass
class ApiError(Exception):
    message: str
    status_code: int = 400
    data: Any = None


def ok(data: Any = None, message: str | None = None) -> dict[str, Any]:
    return {"status": "ok", "message": message, "data": {} if data is None else data}


def error(message: str, data: Any = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "message": message}
    if data is not None:
        payload["data"] = data
    return payload


def unexpected_error(
    prefix: str,
    exc: BaseException,
    *,
    public_message: str | None = None,
) -> dict[str, Any]:
    """Log an unexpected exception and return a client-safe error payload.

    The exception text can carry absolute paths, SQL fragments or upstream
    responses, so it must not be echoed to the browser. Callers get a stable
    message (optionally customised) while the redacted detail stays in logs.

    Args:
        prefix: Log label describing the failed operation.
        exc: The exception that escaped the operation.
        public_message: Message returned to the client; defaults to ``prefix``.

    Returns:
        Error payload safe to return over HTTP.
    """
    detail = redact_sensitive_text(f"{prefix}: {exc!s}")
    logger.error(detail)
    return error(public_message or f"{prefix}，请查看服务端日志了解详情")

