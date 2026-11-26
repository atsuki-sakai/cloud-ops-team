"""A single process-wide value with an optional lifetime.

Replaces ``global`` caches (tokens, downloaded catalogs) so callers mutate an
object instead of rebinding module globals, and tests can ``clear()`` it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class TtlValue(Generic[T]):
    """Hold one value for ``ttl_s`` seconds (forever when ``ttl_s`` is None)."""

    def __init__(self, ttl_s: float | None = None) -> None:
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._value: T | None = None
        self._stored_at = 0.0

    def _fresh(self) -> T | None:
        """The stored value if it has not expired; call with the lock held."""
        if self._value is None:
            return None
        if self._ttl_s is not None and time.time() - self._stored_at >= self._ttl_s:
            return None
        return self._value

    def _store(self, value: T) -> None:
        self._value = value
        self._stored_at = time.time()

    def get(self) -> T | None:
        with self._lock:
            return self._fresh()

    def set(self, value: T) -> None:
        with self._lock:
            self._store(value)

    def get_or_set(self, loader: Callable[[], T]) -> T:
        """Return the cached value, or load and store it (one loader at a time).

        Nothing is stored when ``loader`` raises, so the next call retries.
        """
        with self._lock:
            value = self._fresh()
            if value is None:
                value = loader()
                self._store(value)
            return value

    def clear(self) -> None:
        with self._lock:
            self._value = None
            self._stored_at = 0.0
