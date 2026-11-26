"""JSON parsing helpers shared by request, delivery and evidence decoders."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def unique_pairs_hook(
    error: type[Exception], message: str
) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """An ``object_pairs_hook`` that raises ``error(message)`` on duplicate keys.

    ``json.loads`` silently keeps the last duplicate; callers that must not
    lose or reinterpret fields reject them instead.
    """

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise error(message)
        return result

    return hook
