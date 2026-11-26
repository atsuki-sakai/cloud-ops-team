"""Shared fixtures for unit tests."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest


class ApiCalls:
    """Counts calls to patched API entry points to catch N+1 regressions.

    ``patch(module, "gh_rest", fake)`` replaces ``module.gh_rest`` with a
    wrapper that records ``(name, method/first arg)`` and the peak number of
    calls running at the same time, then delegates to ``fake``.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._monkeypatch = monkeypatch
        self._lock = threading.Lock()
        self._running = 0
        self.calls: Counter[tuple[str, str]] = Counter()
        self.peak_concurrency = 0

    def patch(self, module: Any, name: str, fake: Callable[..., Any]) -> None:
        def counted(*args: Any, **kwargs: Any) -> Any:
            key = (name, str(args[0]) if args else "")
            with self._lock:
                self.calls[key] += 1
                self._running += 1
                self.peak_concurrency = max(self.peak_concurrency, self._running)
            try:
                return fake(*args, **kwargs)
            finally:
                with self._lock:
                    self._running -= 1

        self._monkeypatch.setattr(module, name, counted)

    def count(self, name: str, first_arg: str | None = None) -> int:
        return sum(
            n
            for (called, arg), n in self.calls.items()
            if called == name and (first_arg is None or arg == first_arg)
        )


@pytest.fixture
def api_calls(monkeypatch: pytest.MonkeyPatch) -> ApiCalls:
    return ApiCalls(monkeypatch)
