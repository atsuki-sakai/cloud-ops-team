"""Shared judgement of whether collected evidence is incomplete."""

from __future__ import annotations

from typing import Any

INCOMPLETE_STATUSES: frozenset[str] = frozenset(
    {
        "partial",
        "error",
        "not_supported",
        "permission_denied",
        "auth_error",
        "api_disabled",
        "not_found",
        "rate_limited",
        "invalid_input",
        "validation_error",
    }
)


def is_incomplete(value: Any) -> bool:
    """True if any nested result is marked incomplete or failed."""
    if isinstance(value, dict):
        status = value.get("status")
        if value.get("complete") is False or (
            isinstance(status, str) and status in INCOMPLETE_STATUSES
        ):
            return True
        return any(is_incomplete(v) for v in value.values())
    if isinstance(value, list):
        return any(is_incomplete(v) for v in value)
    return False
