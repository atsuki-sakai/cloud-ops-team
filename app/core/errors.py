"""The one shape for errors that cross a boundary (tool, provider, transport).

Boundaries catch broad exceptions so that one failing collector cannot crash an
agent run. What they return is shown to the model and may be copied into GitHub
issues, so the exception text is redacted and capped here, in one place, rather
than formatted ad hoc at each ``except`` site. ``lint/ast-grep`` rules reject
raw ``str(exc)`` / ``f"{exc}"`` and hand-built ``{"status": "error"}`` dicts in
``except`` blocks so new boundaries go through these helpers.
"""

from __future__ import annotations

from typing import Any

from app.core.redact import redact

MESSAGE_LIMIT = 300


def error_message(exc: BaseException, *, limit: int = MESSAGE_LIMIT) -> str:
    """Return ``"Type: message"`` with secrets masked and the message capped."""
    name = type(exc).__name__
    text = redact(str(exc))[:limit]
    return f"{name}: {text}" if text else name


def tool_error(exc: BaseException, **extra: Any) -> dict[str, Any]:
    """Build the error payload a boundary returns instead of raising.

    ``extra`` adds context (``provider``, ``capability``, ``hint`` ...) and may
    override ``message`` when even redacted exception text must not leave the
    boundary.
    """
    return {
        "status": "error",
        "error_type": type(exc).__name__,
        "message": error_message(exc),
        **extra,
    }
