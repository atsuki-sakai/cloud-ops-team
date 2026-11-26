"""Narrow the ``(ok, payload)`` results of HTTP API wrappers.

``gh_rest`` / ``gcp_request`` / ``gcp_list`` return ``(ok, payload)`` where a
failure always carries an error dict and a success carries the decoded body.
These helpers turn that loose union into the type the caller needs, so every
API caller (GitHub tools and cloud providers alike) handles it the same way.
"""

from __future__ import annotations

from typing import Any

Payload = list[Any] | dict[str, Any]


def failure_payload(payload: Payload, *, source: str = "The API") -> dict[str, Any]:
    """Return the error dict of a failed call; a list means the contract broke."""
    if isinstance(payload, dict):
        return payload
    return {"status": "error", "message": f"{source} returned an unexpected payload."}


def failure_result(
    payload: Payload, *, source: str = "The API", **extra: Any
) -> dict[str, Any]:
    """Build the result a tool or provider returns for a failed API call."""
    error = failure_payload(payload, source=source)
    return {"status": error.get("status", "error"), **extra, "error": error}


def items_payload(payload: Payload, *, source: str = "") -> list[Any]:
    """Return the items of a successful list call."""
    if not isinstance(payload, list):
        prefix = f"{source} " if source else ""
        raise TypeError(f"A successful {prefix}list call must return a list")
    return payload


def object_payload(payload: Payload) -> dict[str, Any]:
    """Return the body of a successful single-object call."""
    if not isinstance(payload, dict):
        raise TypeError("A successful object call must return a dict")
    return payload


def clamp_int(value: float | str, low: int, high: int) -> int:
    """Coerce a model-supplied number into ``[low, high]`` (``int()`` semantics)."""
    return max(low, min(int(value), high))
